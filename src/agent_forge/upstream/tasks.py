"""Task lifecycle shared by every upstream surface.

MCP's ``run_task``/``get_status`` and A2A's task states describe the same thing in two
vocabularies, so there is one implementation and two translations. Building them
separately is how the two surfaces end up disagreeing about whether a task finished.

The authoritative state lives in the graph's checkpointer. This registry holds only what
the checkpointer cannot: the mapping from a task id to its thread, and the terminal
result, so ``get_status`` can answer after the graph has moved on.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from agent_forge.core.errors import TaskNotFoundError
from agent_forge.core.state import AgentState, Identity
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "InMemoryTaskStore",
    "TaskRecord",
    "TaskRunner",
    "TaskState",
    "TaskStore",
]


class TaskState(StrEnum):
    """A2A's task lifecycle, which MCP's status reporting also uses.

    ``INPUT_REQUIRED`` is how a HITL pause is expressed upstream: an orchestrator can then
    show a human its own approval interface instead of the task silently hanging.
    """

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        return self in {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED}


# How an AgentState status maps onto the lifecycle. Kept as data so the two vocabularies
# cannot drift apart in a conditional somewhere.
_FROM_STATUS: dict[str, TaskState] = {
    "running": TaskState.WORKING,
    "awaiting_approval": TaskState.INPUT_REQUIRED,
    "completed": TaskState.COMPLETED,
    "failed": TaskState.FAILED,
    "blocked": TaskState.FAILED,
}


@dataclass(slots=True)
class TaskRecord:
    """One upstream task."""

    task_id: str
    thread_id: str
    tenant_id: str
    state: TaskState = TaskState.SUBMITTED
    question: str = ""
    answer: str = ""
    citations: tuple[str, ...] = ()
    error: str = ""
    classification: str = "C0"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metrics: dict[str, Any] = field(default_factory=dict)

    def apply(self, state: AgentState) -> None:
        """Fold a finished (or paused) graph state into the record."""
        self.state = _FROM_STATUS.get(state.status, TaskState.WORKING)
        self.answer = state.answer
        self.citations = tuple(c.reference for c in state.citations)
        self.classification = str(state.classification)
        self.error = state.blocked_reason or ""
        self.metrics = {
            "latency_ms": state.elapsed_ms(),
            "tokens_in": state.usage.tokens_in,
            "tokens_out": state.usage.tokens_out,
            "cost_usd": state.usage.cost_usd,
            "retries": state.retries,
        }
        self.updated_at = datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "state": str(self.state),
            "answer": self.answer,
            "citations": list(self.citations),
            "error": self.error,
            "classification": self.classification,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metrics": self.metrics,
        }

    def to_a2a(self) -> dict[str, Any]:
        """The A2A task representation.

        Artifacts rather than a bare string: an A2A client renders parts, and citations
        travel as their own part so an orchestrator can show provenance.
        """
        parts: list[dict[str, Any]] = []
        if self.answer:
            parts.append({"kind": "text", "text": self.answer})
        if self.citations:
            parts.append({"kind": "data", "data": {"citations": list(self.citations)}})
        return {
            "id": self.task_id,
            "contextId": self.thread_id,
            "status": {
                "state": str(self.state),
                "timestamp": self.updated_at.isoformat(),
                **(
                    {"message": {"role": "agent", "parts": [{"kind": "text", "text": self.error}]}}
                    if self.error
                    else {}
                ),
            },
            "artifacts": (
                [{"artifactId": f"{self.task_id}-answer", "parts": parts}] if parts else []
            ),
            "kind": "task",
        }


@runtime_checkable
class TaskStore(Protocol):
    """Persistence for upstream task records."""

    async def put(self, record: TaskRecord) -> None: ...

    async def get(self, tenant_id: str, task_id: str) -> TaskRecord | None: ...

    async def list_for(self, tenant_id: str, *, limit: int = 50) -> list[TaskRecord]: ...


class InMemoryTaskStore:
    """Process-local task index.

    Enough for a single-replica cell; the authoritative state is the checkpointer, so
    losing this on restart costs the ability to look a task up by id, not the task.
    """

    def __init__(self, *, max_records: int = 1000) -> None:
        self._records: dict[tuple[str, str], TaskRecord] = {}
        self._max = max_records

    async def put(self, record: TaskRecord) -> None:
        self._records[(record.tenant_id, record.task_id)] = record
        if len(self._records) > self._max:
            oldest = sorted(self._records.items(), key=lambda kv: kv[1].created_at)
            for key, _ in oldest[: len(self._records) - self._max]:
                del self._records[key]

    async def get(self, tenant_id: str, task_id: str) -> TaskRecord | None:
        return self._records.get((tenant_id, task_id))

    async def list_for(self, tenant_id: str, *, limit: int = 50) -> list[TaskRecord]:
        return sorted(
            (r for (t, _), r in self._records.items() if t == tenant_id),
            key=lambda r: r.created_at,
            reverse=True,
        )[:limit]


class TaskRunner:
    """Runs a question through the graph and tracks it as an upstream task.

    Synchronous (``ask``) and asynchronous (``submit`` + ``status``) share one path, so
    the answer an orchestrator gets by polling is the same one it would have got by
    waiting.
    """

    def __init__(
        self,
        invoke: Callable[[AgentState], Awaitable[AgentState]],
        *,
        store: TaskStore | None = None,
        agent_name: str = "",
        area: str = "",
    ) -> None:
        self._invoke = invoke
        self._store = store or InMemoryTaskStore()
        self._agent_name = agent_name
        self._area = area
        self._running: dict[str, asyncio.Task[None]] = {}

    @property
    def store(self) -> TaskStore:
        return self._store

    def build_state(
        self, question: str, identity: Identity, *, thread_id: str = "", task_id: str = ""
    ) -> AgentState:
        from agent_forge.core.state import Message

        return AgentState(
            task_id=task_id or str(uuid.uuid4()),
            thread_id=thread_id or str(uuid.uuid4()),
            trace_id=str(uuid.uuid4()),
            identity=identity,
            agent_name=self._agent_name,
            area=self._area,
            messages=[Message(role="user", content=question)],
        )

    async def ask(self, question: str, identity: Identity, *, thread_id: str = "") -> TaskRecord:
        """Run to completion and return the record. The synchronous path."""
        state = self.build_state(question, identity, thread_id=thread_id)
        record = TaskRecord(
            task_id=state.task_id,
            thread_id=state.thread_id,
            tenant_id=identity.tenant_id,
            question=question,
            state=TaskState.WORKING,
        )
        await self._store.put(record)
        await self._run(record, state)
        return record

    async def submit(self, question: str, identity: Identity, *, thread_id: str = "") -> TaskRecord:
        """Start the task and return immediately. The asynchronous path."""
        state = self.build_state(question, identity, thread_id=thread_id)
        record = TaskRecord(
            task_id=state.task_id,
            thread_id=state.thread_id,
            tenant_id=identity.tenant_id,
            question=question,
            state=TaskState.SUBMITTED,
        )
        await self._store.put(record)

        async def background() -> None:
            record.state = TaskState.WORKING
            await self._run(record, state)

        self._running[record.task_id] = asyncio.create_task(background())
        return record

    async def _run(self, record: TaskRecord, state: AgentState) -> None:
        try:
            final = await self._invoke(state)
        except Exception as exc:
            record.state = TaskState.FAILED
            record.error = f"{type(exc).__name__}: {exc}"
            record.updated_at = datetime.now(UTC)
            log.warning("task.failed", task_id=record.task_id, detail=record.error[:200])
        else:
            record.apply(final)
            log.info("task.finished", task_id=record.task_id, state=str(record.state))
        finally:
            await self._store.put(record)
            self._running.pop(record.task_id, None)

    async def status(self, tenant_id: str, task_id: str) -> TaskRecord:
        record = await self._store.get(tenant_id, task_id)
        if record is None:
            raise TaskNotFoundError("no task with that id for this tenant", task_id=task_id)
        return record

    async def cancel(self, tenant_id: str, task_id: str) -> TaskRecord:
        """Cancel a running task. A finished one is reported, not rewritten."""
        record = await self.status(tenant_id, task_id)
        if record.state.is_terminal:
            return record
        running = self._running.pop(task_id, None)
        if running is not None:
            running.cancel()
        record.state = TaskState.CANCELED
        record.updated_at = datetime.now(UTC)
        await self._store.put(record)
        log.info("task.canceled", task_id=task_id)
        return record

    async def wait(self, task_id: str, *, timeout: float = 30.0) -> None:  # noqa: ASYNC109
        """Wait for a submitted task, for callers that want to poll less."""
        running = self._running.get(task_id)
        if running is not None:
            await asyncio.wait_for(asyncio.shield(running), timeout=timeout)
