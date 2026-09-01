"""The event bus against a real NATS JetStream.

The unit suite runs ``InMemoryBus``, which was written to give the same guarantees. This
runs the ones that can only be checked against the real broker: that a durable consumer
survives a reconnect, that JetStream's own deduplication window catches a republish, that
an unacknowledged message is redelivered, and that a handler which keeps failing ends up in
the dead-letter stream instead of looping forever.

Those four are exactly what a lenient fake would let through, and each of them is a way for
a judge verdict to be lost or applied twice.

Requires Docker. Skipped automatically when it is not available.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from nats import errors as nats_errors

from agent_forge.events.bus import DEAD_LETTER_SUBJECT
from agent_forge.events.nats_impl import DLQ_STREAM_NAME, STREAM_NAME, NatsBus
from agent_forge.events.schemas import (
    JUDGE_VERDICT,
    TASK_RESULT,
    CloudEvent,
    JudgeVerdictData,
    TaskResultData,
)

pytestmark = pytest.mark.integration

OTHER_TENANT = "otra-empresa"
# Long enough for JetStream to redeliver at the ack_wait the test sets, short enough that
# a hung test is noticed.
SETTLE_SECONDS = 1.0


@pytest.fixture(scope="module")
def nats_url() -> Any:
    testcontainers = pytest.importorskip("testcontainers.core.container")
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    try:
        container = testcontainers.DockerContainer("nats:2.14.6-alpine")
        # JetStream is off by default; without it `add_stream` fails and every test here
        # would report a connection problem it does not have.
        container.with_command("-js")
        container.with_exposed_ports(4222)
        container.waiting_for(LogMessageWaitStrategy("Server is ready"))
        container.start()
    except Exception as exc:  # no docker here means skip, not fail
        pytest.skip(f"docker unavailable for integration tests: {exc}")
    try:
        host = container.get_container_host_ip()
        yield f"nats://{host}:{container.get_exposed_port(4222)}"
    finally:
        container.stop()


@pytest.fixture
def tenant(request: pytest.FixtureRequest) -> str:
    """One tenant per test.

    A durable consumer replays its stream from the beginning, so tests sharing a tenant
    also share each other's messages. Separating them here is the same isolation the
    tenant-in-the-subject design gives two real tenants.
    """
    return f"t-{request.node.name[:40].replace('_', '-')}"


@pytest.fixture
async def bus(nats_url: str, tenant: str) -> AsyncIterator[NatsBus]:
    instance = NatsBus(nats_url, source=f"cell-{tenant}")
    await instance.connect()
    yield instance
    await instance.aclose()


def _verdict(task_id: str, *, tenant: str, event_id: str = "") -> CloudEvent:
    return CloudEvent.wrap(
        JUDGE_VERDICT,
        JudgeVerdictData(task_id=task_id, verdict="retry", reasons=["sin citas"]),
        source="judge",
        tenant_id=tenant,
        event_id=event_id,
    )


async def test_the_streams_are_declared_on_connect(bus: NatsBus, nats_url: str) -> None:
    import nats

    connection = await nats.connect(nats_url)
    try:
        js = connection.jetstream()
        names = [info.config.name for info in await js.streams_info()]
    finally:
        await connection.close()

    assert STREAM_NAME in names
    assert DLQ_STREAM_NAME in names


async def test_a_published_verdict_reaches_its_subscriber(bus: NatsBus, tenant: str) -> None:
    received: list[CloudEvent] = []
    await bus.subscribe(JUDGE_VERDICT, _collector(received), tenant_id=tenant)

    await bus.publish(_verdict("t1", tenant=tenant))
    await _settle()

    assert [e.data["task_id"] for e in received] == ["t1"]


async def test_a_subscriber_never_sees_another_tenants_verdict(bus: NatsBus, tenant: str) -> None:
    """The tenant is in the subject, so this is enforced by the broker, not by a filter."""
    received: list[CloudEvent] = []
    await bus.subscribe(JUDGE_VERDICT, _collector(received), tenant_id=tenant)

    await bus.publish(_verdict("mine", tenant=tenant))
    await bus.publish(_verdict("theirs", tenant=OTHER_TENANT))
    await _settle()

    assert [e.data["task_id"] for e in received] == ["mine"]


async def test_a_republished_event_id_is_deduplicated_by_the_broker(
    bus: NatsBus, tenant: str
) -> None:
    """``Nats-Msg-Id`` makes the publish idempotent: a retried publish is not a second event."""
    received: list[CloudEvent] = []
    await bus.subscribe(JUDGE_VERDICT, _collector(received), tenant_id=tenant)
    event = _verdict("t-dedup", tenant=tenant, event_id="fixed-event-id")

    await bus.publish(event)
    await bus.publish(event)
    await _settle()

    assert len(received) == 1


async def test_a_consumer_started_after_the_publish_still_receives_it(
    bus: NatsBus, nats_url: str, tenant: str
) -> None:
    """Persistence is the reason for JetStream: a verdict must not depend on who is connected."""
    await bus.publish(_verdict("t-late", tenant=tenant))

    received: list[CloudEvent] = []
    late = NatsBus(nats_url, source="late-cell")
    try:
        await late.subscribe(JUDGE_VERDICT, _collector(received), tenant_id=tenant)
        await _settle()
    finally:
        await late.aclose()

    assert [e.data["task_id"] for e in received] == ["t-late"]


async def test_a_handler_that_keeps_failing_lands_in_the_dead_letter_stream(
    nats_url: str, tenant: str
) -> None:
    """Not redelivered forever, and not silently dropped: it ends up somewhere readable."""
    import nats

    failing = NatsBus(nats_url, source=f"cell-{tenant}", max_deliveries=2)
    attempts: list[str] = []

    async def always_fails(event: CloudEvent) -> None:
        attempts.append(event.id)
        raise RuntimeError("consumer is broken")

    connection = await nats.connect(nats_url)
    try:
        await failing.subscribe(JUDGE_VERDICT, always_fails, tenant_id=tenant)
        js = connection.jetstream()
        watcher = await _dlq_watcher(js, tenant)

        await failing.publish(_verdict("t-dlq", tenant=tenant))
        dead = await _await_dlq(watcher)
    finally:
        await failing.aclose()
        await connection.close()

    assert len(attempts) >= 2, "the message should have been redelivered before giving up"
    assert dead, "nothing reached the dead-letter stream"
    assert "consumer is broken" in dead


async def test_a_message_the_handler_rejects_once_is_redelivered(
    nats_url: str, tenant: str
) -> None:
    """The reason acknowledgement is explicit: a crash mid-handling must not lose the event."""
    flaky = NatsBus(nats_url, source=f"cell-{tenant}", max_deliveries=5)
    seen: list[int] = []

    async def fails_once(event: CloudEvent) -> None:
        seen.append(len(seen))
        if len(seen) == 1:
            raise RuntimeError("first attempt fails")

    try:
        await flaky.subscribe(TASK_RESULT, fails_once, tenant_id=tenant)
        await flaky.publish(
            CloudEvent.wrap(
                TASK_RESULT,
                TaskResultData(task_id="t-redeliver", tenant_id=tenant),
                source="cell",
                tenant_id=tenant,
            )
        )
        # NatsBus naks a failed handler, and the default ack_wait is long, so the redelivery
        # arrives on the nak rather than on a timer.
        for _ in range(40):
            if len(seen) >= 2:
                break
            await asyncio.sleep(0.25)
    finally:
        await flaky.aclose()

    assert len(seen) >= 2, "a naked message was never redelivered"


async def test_an_undecodable_message_is_dead_lettered_rather_than_retried(
    nats_url: str, tenant: str
) -> None:
    """Redelivering it will not make it parse, so it goes straight to the DLQ."""
    import nats

    from agent_forge.events.bus import subject_for

    bus = NatsBus(nats_url, source=f"cell-{tenant}")
    handled: list[CloudEvent] = []

    connection = await nats.connect(nats_url)
    try:
        await bus.subscribe(TASK_RESULT, _collector(handled), tenant_id=tenant)
        js = connection.jetstream()
        watcher = await _dlq_watcher(js, tenant)
        await js.publish(subject_for(tenant, TASK_RESULT), b"this is not a cloudevent")
        dead = await _await_dlq(watcher)
    finally:
        await bus.aclose()
        await connection.close()

    assert handled == []
    assert "undecodable" in dead


async def _dlq_watcher(js: Any, tenant: str) -> Any:
    """A pull consumer over the dead-letter stream, reading only what arrives from now on.

    ``DeliverPolicy.NEW`` matters: the DLQ stream is shared, and a fresh durable defaults
    to replaying it from the beginning, so the watcher would read some earlier test's
    message and report it as this one's.
    """
    from nats.js.api import ConsumerConfig, DeliverPolicy

    return await js.pull_subscribe(
        f"{DEAD_LETTER_SUBJECT}.>",
        durable=f"watch-{tenant}",
        stream=DLQ_STREAM_NAME,
        config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW),
    )


async def _await_dlq(watcher: Any, attempts: int = 40) -> str:
    """Poll the dead-letter consumer until something arrives, or give up.

    A fetch timeout is the normal "not yet" here, not an error worth reporting: the
    message only appears after the handler has exhausted its deliveries.
    """
    for _ in range(attempts):
        with contextlib.suppress(TimeoutError, nats_errors.TimeoutError):
            messages = await watcher.fetch(1, timeout=1)
            if messages:
                await messages[0].ack()
                return str(messages[0].data.decode())
    return ""


def _collector(sink: list[CloudEvent]) -> Any:
    async def handle(event: CloudEvent) -> None:
        sink.append(event)

    return handle


async def _settle() -> None:
    await asyncio.sleep(SETTLE_SECONDS)
