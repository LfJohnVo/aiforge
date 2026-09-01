"""Authentication and identity resolution.

The rule the whole access-control story rests on: **identity never comes from the message
body**. It comes from a verified OIDC/JWT token or, failing that, from a per-tenant API
key that identifies the *tenant* but not the *user*.

That distinction is the point. An API key alone gets you a tenant and an anonymous
requester, which means a C0 ceiling and no autonomy: enough to ask a public question,
never enough to read the area's confidential documents.
"""

from __future__ import annotations

import hmac
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from agent_forge.core.classification import Classification
from agent_forge.core.errors import AuthenticationError
from agent_forge.core.state import Identity
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

JWKS_CACHE_SECONDS = 300


def parse_api_keys(raw: str) -> dict[str, str]:
    """Parse ``tenant:key,tenant2:key2`` into ``{key: tenant}``.

    Keyed by the secret so lookup is a single constant-time-ish dict hit; the comparison
    itself still uses ``compare_digest``.
    """
    mapping: dict[str, str] = {}
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        tenant, _, key = entry.partition(":")
        tenant, key = tenant.strip(), key.strip()
        if not tenant or not key:
            raise AuthenticationError(
                "AGENT_API_KEYS entry is malformed; expected tenant:key", entry=entry
            )
        mapping[key] = tenant
    return mapping


@dataclass(slots=True)
class AuthSettings:
    """Everything the authenticator needs, read once at startup."""

    api_keys: dict[str, str] = field(default_factory=dict)
    oidc_issuer: str = ""
    oidc_audience: str = "agent-forge"
    jwks_url: str = ""
    # Highest ceiling any authenticated user may reach. The PDP narrows further per
    # request in F6; this is the cell-wide cap.
    max_ceiling: Classification = Classification.C4
    groups_claim: str = "groups"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AuthSettings:
        source = env if env is not None else os.environ
        return cls(
            api_keys=parse_api_keys(source.get("AGENT_API_KEYS", "")),
            oidc_issuer=source.get("OIDC_ISSUER", ""),
            oidc_audience=source.get("OIDC_AUDIENCE", "agent-forge"),
            jwks_url=source.get("OIDC_JWKS_URL", ""),
        )

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.jwks_url)


class Authenticator:
    """Resolves a request's credentials into an ``Identity``."""

    def __init__(self, settings: AuthSettings, *, jwk_client: PyJWKClient | None = None) -> None:
        self._settings = settings
        self._jwk_client = jwk_client
        if jwk_client is None and settings.oidc_enabled:
            self._jwk_client = PyJWKClient(
                settings.jwks_url, cache_keys=True, lifespan=JWKS_CACHE_SECONDS
            )

    @property
    def settings(self) -> AuthSettings:
        return self._settings

    def tenant_for_api_key(self, key: str) -> str | None:
        for known, tenant in self._settings.api_keys.items():
            if hmac.compare_digest(known, key):
                return tenant
        return None

    def authenticate(
        self,
        *,
        authorization: str | None,
        api_key_header: str | None = None,
    ) -> Identity:
        """Resolve the caller. Raises when no credential is valid.

        Order matters: a JWT is strictly more informative than an API key, so it is tried
        first and an API key only fills in the tenant when there is no usable token.
        """
        token = _bearer(authorization)

        if token and self._settings.oidc_enabled:
            return self._identity_from_jwt(token)

        raw_key = api_key_header or token
        if raw_key:
            tenant = self.tenant_for_api_key(raw_key)
            if tenant is not None:
                # Tenant known, user unknown: anonymous, therefore C0 and A0.
                return Identity(
                    tenant_id=tenant,
                    user_id=None,
                    groups=(),
                    classification_ceiling=Classification.C0,
                    authenticated=False,
                )

        raise AuthenticationError(
            "no valid credential presented; send an OIDC bearer token or a tenant API key"
        )

    def _identity_from_jwt(self, token: str) -> Identity:
        assert self._jwk_client is not None  # guarded by oidc_enabled
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self._settings.oidc_audience,
                issuer=self._settings.oidc_issuer,
                options={"require": ["exp", "iss", "sub"]},
            )
        except (jwt.PyJWTError, httpx.HTTPError) as exc:
            log.warning("auth.jwt_rejected", detail=type(exc).__name__)
            raise AuthenticationError("token is not valid for this cell") from exc

        tenant = str(claims.get("tenant_id") or claims.get("tid") or "")
        if not tenant:
            raise AuthenticationError("token carries no tenant claim")
        subject = str(claims.get("sub") or "")
        groups = _claim_list(claims, self._settings.groups_claim)

        return Identity(
            tenant_id=tenant,
            user_id=subject,
            groups=tuple(groups),
            classification_ceiling=self._ceiling_for(claims),
            authenticated=True,
        )

    def _ceiling_for(self, claims: Mapping[str, Any]) -> Classification:
        """Read the ceiling from the token, capped by the cell-wide maximum.

        A token that claims a higher ceiling than the cell allows is clamped, not
        honoured and not rejected: the cap is the cell's decision to make.
        """
        raw = claims.get("classification_ceiling")
        if raw is None:
            return min(Classification.C2, self._settings.max_ceiling)
        try:
            declared = Classification.parse(raw)
        except (ValueError, TypeError):
            log.warning("auth.unparseable_ceiling_claim")
            return Classification.C0
        return min(declared, self._settings.max_ceiling)


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _claim_list(claims: Mapping[str, Any], name: str) -> Sequence[str]:
    value = claims.get(name)
    if isinstance(value, str):
        return [part for part in value.replace(",", " ").split() if part]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def verify_slack_signature(
    signing_secret: str, timestamp: str, body: bytes, signature: str, *, now: float | None = None
) -> bool:
    """Slack request signing (v0), with the replay window Slack specifies.

    Lives here rather than in the Slack channel because signature verification is
    authentication, and authentication belongs in one reviewable place.
    """
    if not signing_secret or not signature.startswith("v0="):
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - sent_at) > 60 * 5:
        return False
    basestring = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(signing_secret.encode(), basestring, "sha256").hexdigest()
    return hmac.compare_digest(expected, signature)
