"""Policy decision points: the local Rego base in Python, and the remote OPA overlay.

Two implementations of one Protocol, as everywhere else in this codebase -- but here the
two are not a real one and a test double. **Both run in production.** The local base
always decides; the remote overlay may only narrow that decision further
(``decisions.combine``). A remote misconfiguration can therefore make the cell stricter,
never more permissive, which is the failure direction worth having.

``LocalPdp`` is a hand translation of ``configs/policies/*.rego``. Duplication, yes, and
deliberate: the cell has to keep deciding when OPA is unreachable, and a Rego interpreter
in the hot path is not a trade worth making. The duplication is held honest by
``tests/policies/cases.py`` -- one table of cases, run through both, so a divergence is a
test failure rather than a surprise in production.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import MAX_EXTERNAL_CLASSIFICATION, Classification
from agent_forge.governance.decisions import PolicyKind, PolicyRequest, PolicyVerdict
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "CachingPdp",
    "LocalPdp",
    "OpaPdp",
    "PolicyDecisionPoint",
]

# Short on purpose: a policy change has to take effect in seconds, not minutes. The cache
# exists to spare the PDP a round trip per retrieved chunk, not to hold decisions.
DEFAULT_TTL_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 2.0


@runtime_checkable
class PolicyDecisionPoint(Protocol):
    """Answers one policy question."""

    async def decide(self, request: PolicyRequest) -> PolicyVerdict: ...

    async def aclose(self) -> None: ...


# --------------------------------------------------------------------------- local


class LocalPdp:
    """The Rego base, evaluated in process.

    Every branch here has a counterpart in ``configs/policies/``. When you change one,
    change the other and add the case to ``tests/policies/cases.py``.
    """

    async def decide(self, request: PolicyRequest) -> PolicyVerdict:
        handler = {
            PolicyKind.KNOWLEDGE: self._knowledge,
            PolicyKind.TOOLS: self._tools,
            PolicyKind.MODELS: self._models,
            PolicyKind.AUTONOMY: self._autonomy,
        }[request.kind]
        return handler(request)

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------- peak.knowledge

    def _knowledge(self, request: PolicyRequest) -> PolicyVerdict:
        denials: list[str] = []
        if not request.authenticated and request.classification > Classification.C0:
            denials.append("anonymous requesters may only read C0 material")
        if request.classification > request.ceiling:
            denials.append(
                f"content is {request.classification} but the requester's ceiling "
                f"is {request.ceiling}"
            )
        if request.acl_groups and not set(request.groups) & set(request.acl_groups):
            denials.append("the document's ACL names groups the requester does not hold")
        if not request.tenant_id:
            denials.append("a request without a tenant cannot be scoped")

        if denials:
            return PolicyVerdict.deny(*denials)
        obligations = ("cite_source",) if request.classification.requires_sovereign_backend else ()
        return PolicyVerdict.permit(
            "knowledge access permitted", ceiling=request.ceiling, obligations=obligations
        )

    # ----------------------------------------------------------------- peak.tools

    def _tools(self, request: PolicyRequest) -> PolicyVerdict:
        denials: list[str] = []
        if not request.authenticated:
            denials.append("an anonymous requester may not invoke tools")
        if not request.resource:
            denials.append("a tool call needs a named tool")
        if not request.tenant_id:
            denials.append("a request without a tenant cannot be scoped")
        if request.classification > request.ceiling:
            denials.append(
                f"the task carries {request.classification}, above the requester's "
                f"ceiling {request.ceiling}"
            )
        if (
            request.classification.requires_sovereign_backend
            and not request.autonomy_declared.requires_approval
        ):
            denials.append("acting on restricted data requires an approved action")

        obligations: list[str] = []
        if request.autonomy_declared.requires_approval:
            obligations += ["human_approval", "ledger_record"]
        if request.autonomy_declared.requires_elevated_role:
            obligations.append("elevated_role")
        if request.autonomy_declared >= AutonomyLevel.A4:
            obligations.append("two_approvers")

        if denials:
            return PolicyVerdict.deny(*denials)
        return PolicyVerdict.permit(
            "tool use permitted",
            obligations=tuple(obligations),
            autonomy=request.autonomy_declared,
        )

    # ---------------------------------------------------------------- peak.models

    def _models(self, request: PolicyRequest) -> PolicyVerdict:
        """The sovereignty invariant, stated a second time.

        ``gateway/model_policy.py`` is the single enforcement point; this is the policy
        that says the same thing where an auditor reads it. They are checked against one
        another by a shared case table, so the two cannot drift.
        """
        denials: list[str] = []
        if request.sovereignty not in {"local", "external"}:
            denials.append("the backend's sovereignty is undeclared")
        if not request.tenant_id:
            denials.append("a request without a tenant cannot be scoped")
        if request.sovereignty == "external":
            if request.classification.requires_sovereign_backend:
                denials.append(
                    f"{request.classification} content may never reach an external backend"
                )
            if request.classification > MAX_EXTERNAL_CLASSIFICATION:
                denials.append(
                    f"an external backend is capped at {MAX_EXTERNAL_CLASSIFICATION} and "
                    f"this request carries {request.classification}"
                )

        obligations = ("ledger_record", "redact_pii") if request.sovereignty == "external" else ()
        if denials:
            return PolicyVerdict.deny(*denials)
        return PolicyVerdict.permit(
            f"{request.sovereignty} backend permitted for {request.classification}",
            obligations=obligations,
            ceiling=request.ceiling,
        )

    # -------------------------------------------------------------- peak.autonomy

    def _autonomy(self, request: PolicyRequest) -> PolicyVerdict:
        if not request.tenant_id:
            return PolicyVerdict.deny("a request without a tenant cannot be scoped")
        granted = self._granted(request)
        obligations = ("human_approval",) if request.autonomy_declared.requires_approval else ()
        if request.autonomy_declared > granted and not request.autonomy_declared.requires_approval:
            # Below A2 there is no approval path, so an action above the grant is simply
            # refused. A2+ is escalated instead: denying it would make approval
            # impossible, which is the opposite of what HITL is for.
            return PolicyVerdict.deny(
                f"the action needs {request.autonomy_declared} but this requester is "
                f"granted {granted}"
            )
        return PolicyVerdict.permit(
            f"autonomy granted up to {granted}", autonomy=granted, obligations=obligations
        )

    @staticmethod
    def _granted(request: PolicyRequest) -> AutonomyLevel:
        if not request.authenticated:
            # A0, not A1: "reversible and low impact" is still an effect, and there is
            # nobody to attribute it to.
            return AutonomyLevel.A0
        if request.classification.requires_sovereign_backend:
            return AutonomyLevel.A0
        return AutonomyLevel.A1


# ----------------------------------------------------------------------- remote


@dataclass(slots=True)
class _Entry:
    verdict: PolicyVerdict
    expires_at: float


class OpaPdp:
    """Client for a remote OPA (or the platform's governance API).

    Queries ``POST {url}/v1/data/peak/{package}/decision`` with the request as ``input``.
    A response OPA cannot produce a decision for comes back as an empty ``result``, which
    this reads as denial -- an absent decision is not an approval.
    """

    def __init__(
        self,
        url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        token: str = "",
    ) -> None:
        self._url = url.rstrip("/")
        self._owns_client = client is None
        headers = {"authorization": f"Bearer {token}"} if token else {}
        self._client = client or httpx.AsyncClient(timeout=timeout, headers=headers)

    async def decide(self, request: PolicyRequest) -> PolicyVerdict:
        path = request.kind.package.replace(".", "/")
        response = await self._client.post(
            f"{self._url}/v1/data/{path}/decision", json={"input": request.to_input()}
        )
        response.raise_for_status()
        payload = response.json()
        if "result" not in payload:
            # OPA answers 200 with no `result` when the rule is undefined. Undefined is
            # not permission.
            log.warning("pdp.undefined", package=request.kind.package)
            return PolicyVerdict.deny(
                f"the remote policy {request.kind.package} produced no decision",
                source="opa",
            )
        return PolicyVerdict.from_opa(payload["result"])

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class CachingPdp:
    """A remote PDP with a short TTL cache and an explicit fail-closed policy.

    What happens when the PDP is unreachable is the whole reason this class exists:

    * **C3/C4 or A2+** -- denied. Always. No cache entry, however fresh, and no
      ``fail_mode`` setting substitutes for a live decision on restricted data or on an
      action with a real-world effect.
    * **``closed``** (the default) -- a fresh cache entry answers; an expired one does
      not.
    * **``permissive_c0c1``** -- an expired entry may answer for C0/C1, flagged ``stale``
      so the ledger records that the decision was not fresh.
    """

    def __init__(
        self,
        remote: PolicyDecisionPoint,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        fail_mode: str = "closed",
        clock: Any = time.monotonic,
    ) -> None:
        self._remote = remote
        self._ttl = ttl_seconds
        self._fail_mode = fail_mode
        self._clock = clock
        self._cache: dict[str, _Entry] = {}

    async def decide(self, request: PolicyRequest) -> PolicyVerdict:
        key = request.cache_key()
        now = self._clock()
        entry = self._cache.get(key)
        if entry is not None and entry.expires_at > now:
            return entry.verdict

        try:
            verdict = await self._remote.decide(request)
        except (httpx.HTTPError, OSError) as exc:
            log.warning(
                "pdp.unreachable",
                kind=str(request.kind),
                detail=f"{type(exc).__name__}: {exc}"[:200],
            )
            return self._fallback(request, entry)

        self._cache[key] = _Entry(verdict=verdict, expires_at=now + self._ttl)
        return verdict

    def _fallback(self, request: PolicyRequest, entry: _Entry | None) -> PolicyVerdict:
        if request.needs_fresh_decision:
            return PolicyVerdict.deny(
                "the policy decision point is unreachable and this request carries "
                f"{max(request.ceiling, request.classification)} or needs "
                f"{request.autonomy_declared}: fail-closed",
                source="opa",
                stale=True,
            )
        if entry is not None and self._fail_mode == "permissive_c0c1":
            return entry.verdict.model_copy(update={"stale": True})
        return PolicyVerdict.deny(
            "the policy decision point is unreachable and no fresh decision is cached",
            source="opa",
            stale=True,
        )

    async def aclose(self) -> None:
        await self._remote.aclose()
