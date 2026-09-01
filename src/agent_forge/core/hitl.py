"""Human in the loop.

An action above the effective autonomy level pauses the graph with LangGraph's
``interrupt()``: the checkpoint is written, the task becomes ``awaiting_approval`` and it
stays that way **indefinitely**. There is no timeout by design -- an A2 action that
expires unattended is an action nobody decided, and silently dropping it is worse than
leaving it visible in the queue.

The store here holds the queue an operator sees at ``/admin/approvals``; the graph's own
resumption is driven by LangGraph's checkpointer, so the two never disagree about
whether an action ran.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.errors import ApprovalError
from agent_forge.core.state import ApprovalRequest
from agent_forge.observability.logging import get_logger
from agent_forge.observability.metrics import get_metrics

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Approver:
    """Who is deciding, and what they are entitled to decide."""

    user_id: str
    groups: tuple[str, ...] = ()

    def belongs_to(self, group: str) -> bool:
        return bool(group) and group in self.groups


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """Which groups may approve, from the profile's governance block."""

    approvers_group: str = ""
    elevated_group: str = ""

    def may_decide(self, approver: Approver, request: ApprovalRequest) -> bool:
        """Elevated actions need the elevated group; everything else the base group.

        When no elevated group is configured, an A3+ action falls back to requiring the
        base approvers group -- never to requiring nothing.
        """
        if request.requires_elevated_role and self.elevated_group:
            return approver.belongs_to(self.elevated_group)
        return approver.belongs_to(self.approvers_group)


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """A queue entry: the request plus enough context for a human to judge it."""

    request: ApprovalRequest
    task_id: str
    thread_id: str
    tenant_id: str
    agent_name: str
    area: str

    def to_public(self) -> dict[str, object]:
        """Shape returned by ``/admin/approvals``. No arguments, only a description."""
        return {
            "id": self.request.id,
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "tenant_id": self.tenant_id,
            "agent_name": self.agent_name,
            "area": self.area,
            "action_category": self.request.action_category,
            "description": self.request.description,
            "autonomy_required": str(self.request.autonomy_required),
            "required_approvals": self.request.required_approvals,
            "approvals": list(self.request.approvals),
            "requires_elevated_role": self.request.requires_elevated_role,
            "created_at": self.request.created_at.isoformat(),
            "status": "pending" if self.request.is_pending else "decided",
        }


class ApprovalStore(abc.ABC):
    """Persistence for the approval queue.

    Abstract because the production implementation is Postgres (it must survive a
    restart) while unit tests use the in-memory one below. Both pass the same contract
    test.
    """

    @abc.abstractmethod
    async def enqueue(self, pending: PendingApproval) -> None: ...

    @abc.abstractmethod
    async def get(self, tenant_id: str, request_id: str) -> PendingApproval | None: ...

    @abc.abstractmethod
    async def list_pending(self, tenant_id: str) -> list[PendingApproval]: ...

    @abc.abstractmethod
    async def update(self, pending: PendingApproval) -> None: ...


class InMemoryApprovalStore(ApprovalStore):
    """Process-local queue. Fine for tests and a single-process dev cell."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], PendingApproval] = {}

    async def enqueue(self, pending: PendingApproval) -> None:
        self._items[(pending.tenant_id, pending.request.id)] = pending

    async def get(self, tenant_id: str, request_id: str) -> PendingApproval | None:
        return self._items.get((tenant_id, request_id))

    async def list_pending(self, tenant_id: str) -> list[PendingApproval]:
        return sorted(
            (p for (t, _), p in self._items.items() if t == tenant_id and p.request.is_pending),
            key=lambda p: p.request.created_at,
        )

    async def update(self, pending: PendingApproval) -> None:
        self._items[(pending.tenant_id, pending.request.id)] = pending
        self._publish(pending.tenant_id)

    def _publish(self, tenant_id: str) -> None:
        """Keep the queue depth gauge honest.

        Recomputed from the queue rather than incremented and decremented: a counter that
        drifts leaves an alert firing about approvals nobody has, and the fix is then to
        restart the process, which is not a fix.
        """
        waiting = sum(
            1 for (t, _), p in self._items.items() if t == tenant_id and p.request.is_pending
        )
        get_metrics().hitl_pending.labels(tenant=tenant_id).set(waiting)


def build_request(
    *,
    description: str,
    autonomy_required: AutonomyLevel,
    action_category: str | None = None,
) -> ApprovalRequest:
    """Create the approval request for an action, deriving its approval requirements."""
    return ApprovalRequest(
        action_category=action_category,
        description=description,
        autonomy_required=autonomy_required,
        required_approvals=autonomy_required.required_approvals,
        requires_elevated_role=autonomy_required.requires_elevated_role,
    )


def apply_approval(
    request: ApprovalRequest, approver: Approver, policy: ApprovalPolicy
) -> ApprovalRequest:
    """Record one approval, returning the updated request.

    Rejects the three ways this goes wrong in practice: the approver is not entitled,
    the request is already decided, and the same person approving twice to satisfy an
    A4 two-approver rule single-handedly.
    """
    if not request.is_pending:
        raise ApprovalError("approval request is already decided", request_id=request.id)
    if not policy.may_decide(approver, request):
        raise ApprovalError(
            "approver is not a member of the group entitled to decide this action",
            request_id=request.id,
            required_group=(
                policy.elevated_group
                if request.requires_elevated_role and policy.elevated_group
                else policy.approvers_group
            ),
        )
    if approver.user_id in request.approvals:
        raise ApprovalError(
            "this approver already approved; a second distinct approver is required",
            request_id=request.id,
            required_approvals=request.required_approvals,
        )

    approvals = (*request.approvals, approver.user_id)
    decided = len(approvals) >= request.required_approvals
    log.info(
        "hitl.approved",
        request_id=request.id,
        approvals=len(approvals),
        required=request.required_approvals,
        granted=decided,
    )
    return request.model_copy(
        update={
            "approvals": approvals,
            "decided_at": datetime.now(UTC) if decided else None,
        }
    )


def apply_rejection(
    request: ApprovalRequest, approver: Approver, policy: ApprovalPolicy, reason: str = ""
) -> ApprovalRequest:
    """Record a rejection. One rejection is final, whatever the approval count."""
    if not request.is_pending:
        raise ApprovalError("approval request is already decided", request_id=request.id)
    if not policy.may_decide(approver, request):
        raise ApprovalError("approver is not entitled to decide this action", request_id=request.id)
    log.info("hitl.rejected", request_id=request.id, reason=reason[:200])
    return request.model_copy(
        update={
            "rejected_by": approver.user_id,
            "reason": reason or None,
            "decided_at": datetime.now(UTC),
        }
    )


def summarise_action(tool: str, args: Sequence[tuple[str, object]] | None = None) -> str:
    """Human-readable description of a pending action, safe to show in a queue.

    Values are summarised, never dumped: an approval queue is a UI, and a UI that
    renders raw tool arguments becomes a place where secrets are read.
    """
    if not args:
        return f"Ejecutar `{tool}`"
    shown = ", ".join(f"{key}={_summarise_value(value)}" for key, value in args)
    return f"Ejecutar `{tool}` con {shown}"


def _summarise_value(value: object) -> str:
    text = str(value)
    if len(text) > 60:
        return f"{text[:57]}..."
    return text
