"""Teams channel: Bot Framework webhook with mandatory signature verification.

A webhook is an unauthenticated door until it checks a signature, so this module is
mostly that check. A request whose token does not verify against Microsoft's JWKS is
dropped without processing and logged -- never answered, never partially handled.

The user's Azure AD object id becomes ``user_id`` and the profile maps their Teams
identity to tenant groups. Without a mapping the requester is authenticated but holds no
groups, which under identity-aware retrieval means they see nothing with an ACL -- the
correct outcome for someone the directory does not place.
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
import jwt
from fastapi import APIRouter, Depends, Header, Request
from jwt import PyJWKClient
from pydantic import BaseModel, Field

from agent_forge.channels.openai_api import get_runtime
from agent_forge.core.classification import Classification
from agent_forge.core.errors import AuthenticationError
from agent_forge.core.state import Identity
from agent_forge.observability.logging import get_logger
from agent_forge.runtime import Runtime

log = get_logger(__name__)

__all__ = ["router", "verify_bot_framework_token"]

router = APIRouter(prefix="/channels/teams", tags=["teams"])

# Microsoft's published keys for Bot Framework channel requests.
JWKS_URL = "https://login.botframework.com/v1/.well-known/keys"
ISSUER = "https://api.botframework.com"


class TeamsActivity(BaseModel):
    """The subset of a Bot Framework activity this channel uses."""

    type: str = "message"
    text: str = ""
    conversation: dict[str, Any] = Field(default_factory=dict)
    from_: dict[str, Any] = Field(default_factory=dict, alias="from")
    channelData: dict[str, Any] = Field(default_factory=dict)  # noqa: N815 - wire format

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @property
    def conversation_id(self) -> str:
        return str(self.conversation.get("id", ""))

    @property
    def user_object_id(self) -> str:
        """The AAD object id, which is stable; ``from.id`` is per-channel and is not."""
        return str(self.from_.get("aadObjectId") or self.from_.get("id") or "")


_jwk_client: PyJWKClient | None = None


def _keys() -> PyJWKClient:
    # One cached JWKS client per process: fetching Microsoft's keys per request
    # would add a round trip to every webhook.
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600)
    return _jwk_client


def verify_bot_framework_token(
    authorization: str | None, *, app_id: str, jwk_client: PyJWKClient | None = None
) -> dict[str, Any]:
    """Verify the Bot Framework JWT. Raises when it does not check out.

    ``app_id`` is the audience: without pinning it, any Bot Framework bot's token would
    be accepted, which is the whole attack.
    """
    if not app_id:
        raise AuthenticationError(
            "Teams is enabled but TEAMS_APP_ID is not set; refusing to accept webhooks"
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Teams webhook is missing its bearer token")

    token = authorization.split(" ", 1)[1].strip()
    client = jwk_client or _keys()
    try:
        signing_key = client.get_signing_key_from_jwt(token)
        return dict(
            jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=app_id,
                issuer=ISSUER,
                options={"require": ["exp", "iss", "aud"]},
            )
        )
    except (jwt.PyJWTError, httpx.HTTPError) as exc:
        log.warning("teams.token_rejected", detail=type(exc).__name__)
        raise AuthenticationError("Teams token failed verification") from exc


def identity_for(
    activity: TeamsActivity, runtime: Runtime, *, group_map: dict[str, list[str]]
) -> Identity:
    """Map the Teams user onto a tenant identity.

    No mapping means no groups, which means no access to anything with an ACL. That is
    correct: the cell must not guess at entitlements for someone the directory has not
    placed.
    """
    user_id = activity.user_object_id
    groups = tuple(group_map.get(user_id, []))
    ceiling = Classification.C2 if groups else Classification.C0
    return Identity(
        tenant_id=runtime.tenant_id,
        user_id=user_id or None,
        groups=groups,
        classification_ceiling=ceiling,
        authenticated=bool(user_id),
    )


@router.post("/messages")
async def messages(
    request: Request,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Handle one Teams activity."""
    settings = runtime.channel_settings.teams
    verify_bot_framework_token(authorization, app_id=settings.app_id)

    activity = TeamsActivity.model_validate(await request.json())
    if activity.type != "message" or not activity.text.strip():
        # Membership changes, typing indicators and the like. Acknowledged, not answered.
        return {"type": "message", "text": ""}

    identity = identity_for(activity, runtime, group_map=settings.group_map)
    record = await runtime.tasks.ask(activity.text, identity, thread_id=activity.conversation_id)
    log.info(
        "teams.answered",
        conversation=activity.conversation_id[:32],
        identified=bool(identity.user_id),
    )
    return {"type": "message", "text": _render(record.answer, record.citations)}


def _render(answer: str, citations: tuple[str, ...]) -> str:
    """Teams renders Markdown; citations go as a short trailing list."""
    if not citations:
        return answer
    sources = "\n".join(f"- `{c}`" for c in citations)
    return f"{answer}\n\n**Fuentes**\n{sources}"
