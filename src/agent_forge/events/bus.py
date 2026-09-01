"""The event bus behind a Protocol, so the broker can be swapped without touching logic.

Two implementations, the usual pair: NATS JetStream for a deployment
(``events/nats_impl.py``) and an in-process bus for a laptop and for tests. Both are held
to the same contract, including the parts that are easy to skip in a fake -- idempotency
by ``event_id`` and a dead-letter path -- because those are exactly the behaviours a test
against a lenient double would let through.

Subjects are ``peak.{tenant}.{type}``. The tenant is in the subject, not only in the
payload: a NATS consumer filters on subject, so this is what lets one tenant's consumer be
unable to receive another tenant's events at all.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from agent_forge.events.schemas import CloudEvent
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "DEAD_LETTER_SUBJECT",
    "EventBus",
    "Handler",
    "InMemoryBus",
    "SeenEvents",
    "subject_for",
]

Handler = Callable[[CloudEvent], Awaitable[None]]

# Outside the `peak.` tree on purpose. JetStream refuses two streams whose subjects
# overlap, and `peak.dlq.>` is a subset of the main stream's `peak.>` -- declaring both
# fails at connect time, on every fresh broker.
DEAD_LETTER_SUBJECT = "peak-dlq"
# How many event ids to remember for deduplication. Sized for a redelivery storm, not for
# history: an event redelivered days later is a different problem.
DEFAULT_SEEN_CAPACITY = 10_000
DEFAULT_MAX_DELIVERIES = 3


def subject_for(tenant_id: str, event_type: str) -> str:
    """``peak.{tenant}.{type}``, with dots in the type flattened.

    NATS treats ``.`` as a subject separator, so a type left as-is would silently create
    a deeper subject tree and a wildcard subscription would stop matching.
    """
    tenant = tenant_id or "_"
    return f"peak.{tenant}.{event_type.replace('.', '-')}"


class SeenEvents:
    """Bounded set of event ids, for at-least-once delivery.

    JetStream redelivers. Without this, a redelivered ``judge.verdict`` re-runs the graph
    a second time -- and a retry verdict processed twice is a task that answers twice.
    """

    __slots__ = ("_capacity", "_seen")

    def __init__(self, capacity: int = DEFAULT_SEEN_CAPACITY) -> None:
        self._capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()

    def add(self, event_id: str) -> bool:
        """Record the id. Returns False when it had already been seen.

        Call this **after** the handler succeeds, never before. Recording on receipt
        deduplicates the redelivery of a message the handler failed on, which turns
        at-least-once delivery into at-most-once and loses the event for good.
        """
        if event_id in self._seen:
            self._seen.move_to_end(event_id)
            return False
        self._seen[event_id] = None
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return True

    def __contains__(self, event_id: object) -> bool:
        return event_id in self._seen

    def __len__(self) -> int:
        return len(self._seen)


@runtime_checkable
class EventBus(Protocol):
    """Publish/subscribe over CloudEvents."""

    async def publish(self, event: CloudEvent) -> None: ...

    async def subscribe(
        self, event_type: str, handler: Handler, *, tenant_id: str = ""
    ) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class _Subscription:
    subject: str
    handler: Handler


@dataclass(slots=True)
class InMemoryBus:
    """In-process bus with the same guarantees the real one gives.

    Not a stub: it deduplicates by event id and dead-letters a handler that keeps
    failing, because those are the behaviours the aggregator and the judge depend on.
    Running the cell standalone must exercise the same paths a deployment does.
    """

    max_deliveries: int = DEFAULT_MAX_DELIVERIES
    _subs: list[_Subscription] = field(default_factory=list, init=False)
    _seen: SeenEvents = field(default_factory=SeenEvents, init=False)
    dead_letters: list[CloudEvent] = field(default_factory=list, init=False)
    published: list[CloudEvent] = field(default_factory=list, init=False)

    async def publish(self, event: CloudEvent) -> None:
        subject = subject_for(event.tenantid, event.type)
        self.published.append(event)
        handlers = [s.handler for s in self._subs if _matches(s.subject, subject)]
        if not handlers:
            return
        if event.id in self._seen:
            log.info("bus.duplicate_ignored", event_id=event.id, type=event.type)
            return
        delivered = [await self._deliver(handler, event) for handler in handlers]
        # Recorded only once the event was actually handled. Marking it on receipt would
        # deduplicate its own redelivery and lose it.
        if all(delivered):
            self._seen.add(event.id)

    async def _deliver(self, handler: Handler, event: CloudEvent) -> bool:
        """Deliver with retries. Returns whether the handler eventually accepted it."""
        for attempt in range(1, self.max_deliveries + 1):
            try:
                await handler(event)
            except Exception as exc:  # a bad handler must not take the publisher down
                log.warning(
                    "bus.handler_failed",
                    event_id=event.id,
                    attempt=attempt,
                    detail=f"{type(exc).__name__}: {exc}"[:200],
                )
                if attempt == self.max_deliveries:
                    self.dead_letters.append(event)
                    log.error("bus.dead_lettered", event_id=event.id, type=event.type)
                await asyncio.sleep(0)
            else:
                return True
        return False

    async def subscribe(self, event_type: str, handler: Handler, *, tenant_id: str = "") -> None:
        self._subs.append(
            _Subscription(subject=subject_for(tenant_id or "*", event_type), handler=handler)
        )
        log.info("bus.subscribed", type=event_type, tenant=tenant_id or "*")

    async def aclose(self) -> None:
        self._subs.clear()

    def events_of(self, event_type: str) -> list[CloudEvent]:
        """Published events of one type. For tests and for the local aggregator."""
        return [e for e in self.published if e.type == event_type]


def _matches(pattern: str, subject: str) -> bool:
    """NATS-style token matching, limited to the ``*`` wildcard this bus uses."""
    left, right = pattern.split("."), subject.split(".")
    if len(left) != len(right):
        return False
    return all(a == "*" or a == b for a, b in zip(left, right, strict=True))
