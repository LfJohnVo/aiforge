"""The governance layer the graph calls.

One service answers all four questions -- knowledge, tools, models, autonomy -- because
they share the same inputs and the same failure policy, and because they all have to land
in the same ledger with the same shape.

The order inside the gate is deliberate: **DLP first, PDP second.** DLP inspects text and
can end the request outright; the PDP decides about facts. Asking the PDP about a request
that is going to be blocked anyway spends a round trip and, worse, puts the unscrubbed
text one step closer to a remote service.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import Classification
from agent_forge.core.graph import GateOutcome
from agent_forge.core.state import AgentState, Identity
from agent_forge.governance.decisions import (
    PolicyKind,
    PolicyRequest,
    PolicyVerdict,
    combine,
)
from agent_forge.governance.dlp import DlpEngine, DlpResult
from agent_forge.governance.pdp import CachingPdp, LocalPdp, OpaPdp, PolicyDecisionPoint
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "CachingPdp",
    "DlpEngine",
    "GovernanceService",
    "LocalPdp",
    "OpaPdp",
    "PolicyDecisionPoint",
    "PolicyKind",
    "PolicyRequest",
    "PolicyVerdict",
    "build_governance",
]

# Called with (action, actor, payload) when a decision must be recorded. The ledger
# supplies it; without one, governance still works and simply records nothing.
LedgerHook = Callable[[str, str, dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class GovernanceService:
    """PDP + DLP behind the graph's ``GovernanceHook``."""

    local: PolicyDecisionPoint = field(default_factory=LocalPdp)
    remote: PolicyDecisionPoint | None = None
    dlp: DlpEngine = field(default_factory=DlpEngine.disabled)
    ledger: LedgerHook | None = None

    # ------------------------------------------------------------------ the gate

    async def gate(self, state: AgentState) -> GateOutcome:
        """The entry gate: scan the user's text, then decide what they may reach."""
        question = state.last_user_message
        scan = self.dlp.scan(question, "input", tenant_id=state.identity.tenant_id)
        if scan.blocked:
            await self._record("policy_decision", state, allow=False, reasons=scan.reasons())
            return GateOutcome(
                allow=False,
                classification_ceiling=Classification.C0,
                autonomy_granted=AutonomyLevel.A0,
                reasons=scan.reasons(),
            )

        verdict = await self.decide(
            PolicyRequest.for_identity(
                PolicyKind.AUTONOMY,
                state.identity,
                classification=state.classification,
            )
        )
        await self._record("policy_decision", state, allow=verdict.allow, reasons=verdict.reasons)

        if not verdict.allow:
            return GateOutcome(
                allow=False,
                classification_ceiling=Classification.C0,
                autonomy_granted=AutonomyLevel.A0,
                reasons=verdict.reasons,
                obligations=verdict.obligations,
                fail_closed=verdict.stale,
            )

        ceiling = _ceiling_for(state.identity, verdict)
        return GateOutcome(
            allow=True,
            classification_ceiling=ceiling,
            autonomy_granted=verdict.autonomy or AutonomyLevel.A0,
            reasons=verdict.reasons + _dlp_reasons(scan),
            obligations=verdict.obligations,
            fail_closed=verdict.stale,
            redacted_text=scan.text if scan.redacted else None,
        )

    # ------------------------------------------------------------ the four asks

    async def decide(self, request: PolicyRequest) -> PolicyVerdict:
        """Local base, then remote overlay, combined the restrictive way."""
        base = await self.local.decide(request)
        if self.remote is None:
            return base
        overlay = await self.remote.decide(request)
        return combine(base, overlay)

    async def may_see(
        self,
        identity: Identity,
        *,
        classification: Classification,
        acl_groups: Sequence[str] = (),
        source: str = "",
    ) -> PolicyVerdict:
        return await self.decide(
            PolicyRequest.for_identity(
                PolicyKind.KNOWLEDGE,
                identity,
                classification=classification,
                acl_groups=tuple(acl_groups),
                resource=source,
            )
        )

    async def may_use_tool(
        self,
        identity: Identity,
        *,
        tool: str,
        classification: Classification,
        autonomy: AutonomyLevel,
        action_category: str = "",
    ) -> PolicyVerdict:
        return await self.decide(
            PolicyRequest.for_identity(
                PolicyKind.TOOLS,
                identity,
                resource=tool,
                classification=classification,
                autonomy_declared=autonomy,
                action_category=action_category,
            )
        )

    async def may_send_to(
        self,
        identity: Identity,
        *,
        model: str,
        sovereignty: str,
        classification: Classification,
    ) -> PolicyVerdict:
        """Whether this content may go to this backend.

        A second opinion, not the enforcement point. ``gateway/model_policy.py`` decides,
        and it decides alone; this exists so the same rule is auditable as policy and so a
        platform overlay can be *stricter* than the built-in cap.
        """
        return await self.decide(
            PolicyRequest.for_identity(
                PolicyKind.MODELS,
                identity,
                resource=model,
                sovereignty=sovereignty,
                classification=classification,
            )
        )

    # ------------------------------------------------------------------- output

    def scan_answer(self, text: str, *, tenant_id: str = "") -> DlpResult:
        """Outbound scan. A secret here was already inside the tenant's corpus."""
        return self.dlp.scan(text, "output", tenant_id=tenant_id)

    async def aclose(self) -> None:
        await self.local.aclose()
        if self.remote is not None:
            await self.remote.aclose()

    # ------------------------------------------------------------------ helpers

    async def _record(self, action: str, state: AgentState, **payload: Any) -> None:
        if self.ledger is None:
            return
        await self.ledger(
            action,
            "agent",
            {"task_id": state.task_id, "tenant_id": state.identity.tenant_id, **payload},
        )


def _ceiling_for(identity: Identity, verdict: PolicyVerdict) -> Classification:
    """The requester's ceiling, narrowed by whatever policy says. Never widened."""
    if verdict.ceiling is None:
        return identity.classification_ceiling
    return min(identity.classification_ceiling, verdict.ceiling)


def _dlp_reasons(scan: DlpResult) -> tuple[str, ...]:
    if not scan.redacted:
        return ()
    return (f"input redacted: {', '.join(scan.reasons()) or 'pii'}",)


def build_governance(
    profile: Any,
    *,
    client: Any | None = None,
    ledger: LedgerHook | None = None,
) -> GovernanceService:
    """Assemble the service from the profile.

    No PDP URL means local policy only. That is a supported deployment -- a cell on a
    laptop, or an isolated site -- not a degraded one: the local Rego base is the same
    base a remote overlay would narrow.
    """
    settings = profile.governance
    dlp = (
        DlpEngine.from_file(settings.dlp.rules_file)
        if settings.dlp.enabled
        else DlpEngine.disabled()
    )
    remote: PolicyDecisionPoint | None = None
    if settings.pdp_url:
        remote = CachingPdp(
            OpaPdp(settings.pdp_url, client=client),
            fail_mode=settings.fail_mode,
        )
    else:
        log.info("governance.local_only", detail="no pdp_url configured; local Rego base only")
    return GovernanceService(local=LocalPdp(), remote=remote, dlp=dlp, ledger=ledger)
