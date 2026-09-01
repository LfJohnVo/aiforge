"""``AgentState``: everything the graph carries between nodes.

The state is serialised to the checkpointer at every node boundary, so it has to be
fully typed and fully serialisable. It is also what a task resumes from hours later
after a human approval, which is why identity and the cumulative classification live
here and not in some request-scoped object that would be gone by then.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import Classification, accumulate

TaskStatus = Literal["running", "awaiting_approval", "completed", "failed", "blocked"]
Verdict = Literal["approve", "retry", "replan", "escalate"]


@dataclass(frozen=True, slots=True)
class QualityVerdict:
    """What the judge concluded about an answer.

    Lives here rather than in ``core/graph.py`` so the judge that produces it does not
    have to import the graph that consumes it -- an edge that made ``events`` depend on
    ``core.graph`` and closed a cycle through the observability package.

    The verdict, not just the scores: the thresholds live with the judge, where the
    profile configures them, so the quality gate does not need to know what "good enough"
    means for this deployment. Its job is the retry budget and the routing.
    """

    verdict: Verdict = "approve"
    scores: dict[str, float] = dc_field(default_factory=dict)
    reasons: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.verdict == "approve"


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid.uuid4())


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Identity(_Model):
    """Who is asking. The only source is a verified token, never the message body."""

    tenant_id: str
    user_id: str | None = None
    groups: tuple[str, ...] = ()
    # Highest classification this requester may ever see. Without a verified identity
    # this is C0, which is what makes anonymous access safe by construction.
    classification_ceiling: Classification = Classification.C0
    authenticated: bool = False

    @property
    def is_anonymous(self) -> bool:
        return not self.authenticated or self.user_id is None


class Message(_Model):
    """One turn of conversation."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    created_at: datetime = Field(default_factory=_now)


class Citation(_Model):
    """A pointer back to the source that supports a claim."""

    source_id: str
    chunk_id: str
    title: str = ""
    score: float = 0.0
    classification: Classification = Classification.C0

    @property
    def reference(self) -> str:
        return f"{self.source_id}#{self.chunk_id}"


class PlanStep(_Model):
    """One step the planner produced."""

    id: str = Field(default_factory=_new_id)
    description: str
    action_category: str | None = None
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "running", "done", "skipped", "failed"] = "pending"
    result_digest: str | None = None


class ToolInvocation(_Model):
    """Record of a tool call, kept in state so a resumed task knows what already ran."""

    id: str = Field(default_factory=_new_id)
    connector: str
    tool: str
    args_digest: str
    ok: bool = False
    error: str | None = None
    classification: Classification = Classification.C0
    autonomy_required: AutonomyLevel = AutonomyLevel.A0
    approved_by: tuple[str, ...] = ()
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None


class ApprovalRequest(_Model):
    """A paused action waiting for a human.

    Lives in the state (not only in Postgres) so that resuming from a checkpoint
    restores the pending decision exactly as it was.
    """

    id: str = Field(default_factory=_new_id)
    action_category: str | None
    description: str
    autonomy_required: AutonomyLevel
    required_approvals: int = 1
    requires_elevated_role: bool = False
    approvals: tuple[str, ...] = ()
    rejected_by: str | None = None
    reason: str | None = None
    created_at: datetime = Field(default_factory=_now)
    decided_at: datetime | None = None

    @property
    def is_pending(self) -> bool:
        return self.decided_at is None and self.rejected_by is None

    @property
    def is_granted(self) -> bool:
        return self.rejected_by is None and len(self.approvals) >= self.required_approvals


class PolicyDecision(_Model):
    """A recorded governance decision, for the ledger and for the trace."""

    kind: Literal["knowledge_access", "tool_use", "external_model", "autonomy", "dlp"]
    allow: bool
    reasons: tuple[str, ...] = ()
    obligations: tuple[str, ...] = ()
    cached: bool = False
    fail_closed: bool = False
    at: datetime = Field(default_factory=_now)


class Usage(_Model):
    """Token and cost accounting, reported in ``task.result`` and in metrics."""

    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    model_calls: int = 0
    cache_hits: int = 0

    def add(self, *, tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0) -> Usage:
        """Return a new Usage with the call folded in (state updates stay immutable)."""
        return Usage(
            tokens_in=self.tokens_in + tokens_in,
            tokens_out=self.tokens_out + tokens_out,
            cost_usd=round(self.cost_usd + cost_usd, 6),
            model_calls=self.model_calls + 1,
            cache_hits=self.cache_hits,
        )


def _merge_messages(left: list[Message], right: list[Message]) -> list[Message]:
    """LangGraph reducer: nodes append to the transcript, never rewrite it."""
    return [*left, *right]


def _merge_unique(left: list[Any], right: list[Any]) -> list[Any]:
    """Append, dropping repeats. Citations and decisions can arrive twice on retry."""
    seen = {_key(item) for item in left}
    merged = list(left)
    for item in right:
        key = _key(item)
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


def _key(item: Any) -> str:
    if isinstance(item, Citation):
        return item.reference
    if isinstance(item, BaseModel):
        return repr(sorted(item.model_dump(mode="json").items()))
    return repr(item)


class AgentState(BaseModel):
    """The graph's state. One instance per task."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    # ---- correlation
    task_id: str = Field(default_factory=_new_id)
    thread_id: str = Field(default_factory=_new_id)
    trace_id: str = ""

    # ---- who and what
    identity: Identity
    agent_name: str = ""
    area: str = ""
    messages: Annotated[list[Message], _merge_messages] = Field(default_factory=list)

    # ---- governance
    # Maximum of the request, every retrieved chunk and every tool result. This is the
    # value the model gateway reads; nothing else may decide a backend.
    classification: Classification = Classification.C0
    autonomy: AutonomyLevel = AutonomyLevel.A0
    policy_decisions: Annotated[list[PolicyDecision], _merge_unique] = Field(default_factory=list)
    blocked_reason: str | None = None

    # ---- work
    plan: list[PlanStep] = Field(default_factory=list)
    tool_calls: Annotated[list[ToolInvocation], _merge_unique] = Field(default_factory=list)
    citations: Annotated[list[Citation], _merge_unique] = Field(default_factory=list)
    scratchpad: dict[str, Any] = Field(default_factory=dict)

    # ---- human in the loop
    approvals: list[ApprovalRequest] = Field(default_factory=list)

    # ---- outcome
    answer: str = ""
    status: TaskStatus = "running"
    verdict: Verdict | None = None
    judge_scores: dict[str, float] = Field(default_factory=dict)
    retries: int = 0
    usage: Usage = Field(default_factory=Usage)
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None

    # ------------------------------------------------------------- derivations

    @property
    def last_user_message(self) -> str:
        for message in reversed(self.messages):
            if message.role == "user":
                return message.content
        return ""

    @property
    def pending_approval(self) -> ApprovalRequest | None:
        return next((a for a in self.approvals if a.is_pending), None)

    @property
    def is_terminal(self) -> bool:
        return self.status in {"completed", "failed", "blocked"}

    def with_classification(self, *values: Classification | str | int | None) -> Classification:
        """Fold new classifications into the running maximum and return it.

        Every place that introduces external content -- retrieval, tool results -- must
        call this. Skipping it is how classified data ends up routed to a model that
        should never have seen it.
        """
        return accumulate(self.classification, *values)

    def elapsed_ms(self) -> int:
        end = self.finished_at or _now()
        return int((end - self.started_at).total_seconds() * 1000)

    def summary(self) -> dict[str, Any]:
        """Compact, non-sensitive view for logs, metrics and ``task.result``."""
        return {
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "tenant_id": self.identity.tenant_id,
            "agent_name": self.agent_name,
            "area": self.area,
            "status": self.status,
            "classification": str(self.classification),
            "autonomy": str(self.autonomy),
            "verdict": self.verdict,
            "citations": [c.reference for c in self.citations],
            "retries": self.retries,
            "latency_ms": self.elapsed_ms(),
            "tokens_in": self.usage.tokens_in,
            "tokens_out": self.usage.tokens_out,
            "cost_usd": self.usage.cost_usd,
        }
