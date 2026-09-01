"""Publishing task results and acting on judge verdicts.

The cell's half of the closing loop. It publishes ``task.result`` when a task ends and
subscribes to ``judge.verdict``; the aggregator and the central judge are platform
services, and in ``standalone`` mode the local judge stands in for them.

The part that needs care is what a verdict *does*:

* ``approve``   -- nothing. The answer already went out.
* ``retry``     -- resume the graph from its checkpoint, same plan.
* ``replan``    -- resume from the checkpoint with the plan discarded.
* ``escalate``  -- raise a human approval instead of another attempt.

Resuming from the checkpoint and not re-running from scratch is the whole reason the
graph is checkpointed. A verdict arrives seconds or minutes later, over a bus, in a
different process than the one that produced the answer; re-running the task would repeat
every tool call it already made.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_forge.events.bus import EventBus
from agent_forge.events.evidence import digest
from agent_forge.events.schemas import (
    JUDGE_VERDICT,
    TASK_RESULT,
    CloudEvent,
    JudgeVerdictData,
    TaskResultData,
)
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = ["Aggregator", "result_event"]


def result_event(state: Any, *, source: str, agent_id: str = "") -> CloudEvent:
    """Build ``com.peak.task.result.v1`` from a finished state.

    The answer travels as a **digest**, never as text. The fabric is shared
    infrastructure; the aggregator needs to correlate, not to read.
    """
    data = TaskResultData(
        task_id=state.task_id,
        trace_id=state.trace_id,
        tenant_id=state.identity.tenant_id,
        agent_id=agent_id or state.agent_name,
        area=state.area,
        autonomy_level=str(state.autonomy),
        classification=str(state.classification),
        output={
            "answer_digest": digest(state.answer),
            "citations": [c.reference for c in state.citations],
            "artifacts": [],
        },
        metrics={
            "latency_ms": state.elapsed_ms(),
            "tokens_in": state.usage.tokens_in,
            "tokens_out": state.usage.tokens_out,
            "cost_usd": state.usage.cost_usd,
            "retries": state.retries,
        },
        status=_status_of(state),
    )
    return CloudEvent.wrap(TASK_RESULT, data, source=source, tenant_id=state.identity.tenant_id)


def _status_of(state: Any) -> str:
    if state.status in {"completed", "failed", "awaiting_approval"}:
        return str(state.status)
    # `blocked` and anything else unfinished report as failed upstream: the aggregator's
    # vocabulary has three words and inventing a fourth breaks its consumers.
    return "failed"


@dataclass(slots=True)
class Aggregator:
    """Publishes results, consumes verdicts, resumes the graph."""

    bus: EventBus
    source: str = "agent-forge"
    tenant_id: str = ""
    # (task_id, verdict, reasons) -> None. Supplied by the runtime; it owns the graph and
    # the checkpointer, which this module deliberately does not.
    resume: Any | None = None
    escalate: Any | None = None
    ledger: Any | None = None
    handled: list[str] = field(default_factory=list, init=False)

    async def publish_result(self, state: Any, *, agent_id: str = "") -> CloudEvent:
        event = result_event(state, source=self.source, agent_id=agent_id)
        await self.bus.publish(event)
        if self.ledger is not None:
            await self.ledger.record(
                "task_result",
                "agent",
                event.data,
                tenant_id=state.identity.tenant_id,
                metadata={"task_id": state.task_id, "status": event.data.get("status")},
            )
        log.info("aggregator.published", task_id=state.task_id, status=event.data.get("status"))
        return event

    async def listen(self) -> None:
        """Subscribe to verdicts for this tenant."""
        await self.bus.subscribe(JUDGE_VERDICT, self.on_verdict, tenant_id=self.tenant_id)

    async def on_verdict(self, event: CloudEvent) -> None:
        """Apply one judge verdict."""
        verdict = JudgeVerdictData.model_validate(event.data)
        self.handled.append(verdict.task_id)
        log.info(
            "aggregator.verdict",
            task_id=verdict.task_id,
            verdict=verdict.verdict,
            score=verdict.score,
        )
        if self.ledger is not None:
            await self.ledger.record(
                "verdict",
                "judge",
                event.data,
                tenant_id=event.tenantid or self.tenant_id,
                metadata={"task_id": verdict.task_id, "verdict": verdict.verdict},
            )

        if verdict.verdict == "approve":
            return
        if verdict.verdict == "escalate":
            if self.escalate is not None:
                await self.escalate(verdict.task_id, tuple(verdict.reasons))
            return
        if self.resume is None:
            # No resume hook means this deployment cannot act on a retry. Say so loudly:
            # silently dropping it would leave the platform waiting for a second result.
            log.error(
                "aggregator.cannot_resume",
                task_id=verdict.task_id,
                verdict=verdict.verdict,
                detail="no resume hook is wired; the verdict was recorded and ignored",
            )
            return
        await self.resume(verdict.task_id, verdict.verdict, tuple(verdict.reasons))
