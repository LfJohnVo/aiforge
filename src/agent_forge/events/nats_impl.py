"""NATS JetStream implementation of the event bus.

JetStream and not core NATS because the cell's events are not telemetry: a
``judge.verdict`` lost because no consumer happened to be connected is a task that never
finishes. Persistence, durable consumers and explicit acknowledgement are the point.

Three things here are worth reading rather than skimming, because they are what make
at-least-once delivery survivable:

* **Idempotency by ``event_id``, recorded after the handler succeeds.** JetStream
  redelivers, and a redelivered retry verdict would re-run a task that already ran. But the
  id must be recorded *after* handling, not on receipt: recording it first deduplicates the
  redelivery of a message the handler failed on, which silently converts at-least-once
  delivery into at-most-once.
* **Explicit ack after the handler returns.** Acknowledging on receipt would lose the
  event if the process died mid-handling, which is the moment it matters most.
* **A real DLQ.** After ``max_deliver`` attempts the message goes to ``peak-dlq.*`` with
  the failure attached, instead of being redelivered forever or silently discarded. It is
  its own stream, outside the ``peak.`` tree, because JetStream refuses overlapping
  subjects.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agent_forge.events.bus import (
    DEAD_LETTER_SUBJECT,
    DEFAULT_MAX_DELIVERIES,
    Handler,
    SeenEvents,
    subject_for,
)
from agent_forge.events.schemas import CloudEvent
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = ["STREAM_NAME", "BrokerUnavailableError", "NatsBus"]


class BrokerUnavailableError(ConnectionError):
    """The broker is configured but not reachable.

    Its own type so a caller can tell "the fabric is down" from "the message was rejected".
    The first is a degradation the cell rides out; the second is a defect.
    """


STREAM_NAME = "PEAK"
DLQ_STREAM_NAME = "PEAK_DLQ"
# Long enough for a slow judge, short enough that a stuck consumer is noticed.
ACK_WAIT_SECONDS = 60.0
MAX_AGE_SECONDS = 7 * 24 * 3600
# Connecting must FAIL, not hang. `nats.connect` defaults to retrying for ever, and a
# broker that is configured but absent -- a `core` profile with NATS_URL still set, a DNS
# entry that has not propagated -- then blocks the first publish, which happens inside a
# request. Observed exactly that: the cell answered nothing while the client retried DNS
# every four seconds. Bounded here, and the failure is reported.
CONNECT_TIMEOUT_SECONDS = 3
CONNECT_ATTEMPTS = 2
PUBLISH_TIMEOUT_SECONDS = 5.0


class NatsBus:
    """CloudEvents over JetStream."""

    def __init__(
        self,
        url: str,
        *,
        source: str = "agent-forge",
        max_deliveries: int = DEFAULT_MAX_DELIVERIES,
    ) -> None:
        self._url = url
        self._source = source
        self._max_deliveries = max_deliveries
        self._nc: Any = None
        self._js: Any = None
        self._seen = SeenEvents()
        self._subscriptions: list[Any] = []
        # Set once a connect attempt has failed, so the next publish does not pay the
        # timeout again. A cell whose broker is down must degrade, not slow down.
        self._unreachable = False

    # ---------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        """Connect and declare the streams. Idempotent, bounded, and it can give up.

        Raises ``BrokerUnavailableError`` rather than blocking. The caller decides what an
        absent broker means: for the ledger it means "the local chain is still the source
        of truth"; for the aggregator it means the platform will not hear about this task.
        Neither of those is a reason to stall a user's request.
        """
        if self._nc is not None:
            return
        if self._unreachable:
            raise BrokerUnavailableError(
                f"{_safe(self._url)} was unreachable on a previous attempt"
            )

        import nats
        from nats.js.api import RetentionPolicy, StreamConfig

        try:
            self._nc = await nats.connect(
                self._url,
                name=self._source,
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                max_reconnect_attempts=CONNECT_ATTEMPTS,
                allow_reconnect=True,
            )
        except Exception as exc:
            self._unreachable = True
            log.warning(
                "nats.unreachable",
                url=_safe(self._url),
                detail=f"{type(exc).__name__}: {exc}"[:200],
            )
            raise BrokerUnavailableError(str(exc)[:200]) from exc
        self._js = self._nc.jetstream()

        for name, subjects in (
            (STREAM_NAME, ["peak.>"]),
            (DLQ_STREAM_NAME, [DEAD_LETTER_SUBJECT + ".>"]),
        ):
            await self._js.add_stream(
                StreamConfig(
                    name=name,
                    subjects=subjects,
                    retention=RetentionPolicy.LIMITS,
                    max_age=MAX_AGE_SECONDS,
                    # JetStream's own dedup window, on top of `SeenEvents`. This one
                    # catches a publisher retrying; the other catches a consumer
                    # redelivery. They are different failures.
                    duplicate_window=120.0,
                )
            )
        log.info("nats.connected", url=_safe(self._url), streams=[STREAM_NAME, DLQ_STREAM_NAME])

    async def aclose(self) -> None:
        for sub in self._subscriptions:
            try:
                await sub.unsubscribe()
            except Exception as exc:  # shutting down; there is nothing left to recover
                log.debug("nats.unsubscribe_failed", detail=str(exc)[:120])
        self._subscriptions.clear()
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None
            self._js = None

    # ------------------------------------------------------------------ publish

    async def publish(self, event: CloudEvent) -> None:
        await self.connect()
        subject = subject_for(event.tenantid, event.type)
        payload = event.model_dump_json().encode()
        # `Nats-Msg-Id` is what makes the publish itself idempotent: a retry after a
        # timeout that actually succeeded does not produce a second message.
        await asyncio.wait_for(
            self._js.publish(subject, payload, headers={"Nats-Msg-Id": event.id}),
            timeout=PUBLISH_TIMEOUT_SECONDS,
        )
        log.debug("nats.published", subject=subject, event_id=event.id, type=event.type)

    # ---------------------------------------------------------------- subscribe

    async def subscribe(self, event_type: str, handler: Handler, *, tenant_id: str = "") -> None:
        """Durable push subscription with manual acknowledgement."""
        await self.connect()
        from nats.js.api import AckPolicy, ConsumerConfig

        subject = subject_for(tenant_id or "*", event_type)
        durable = _durable_name(self._source, event_type, tenant_id)

        async def on_message(msg: Any) -> None:
            await self._handle(msg, handler)

        subscription = await self._js.subscribe(
            subject,
            durable=durable,
            cb=on_message,
            manual_ack=True,
            config=ConsumerConfig(
                durable_name=durable,
                ack_policy=AckPolicy.EXPLICIT,
                ack_wait=ACK_WAIT_SECONDS,
                max_deliver=self._max_deliveries,
                filter_subject=subject,
            ),
        )
        self._subscriptions.append(subscription)
        log.info("nats.subscribed", subject=subject, durable=durable)

    async def _handle(self, msg: Any, handler: Handler) -> None:
        try:
            event = CloudEvent.model_validate_json(msg.data)
        except ValueError as exc:
            # Unparseable: redelivering it will not make it parse. Straight to the DLQ.
            log.error("nats.undecodable", detail=str(exc)[:200])
            await self._dead_letter_raw(msg, reason="undecodable")
            await msg.ack()
            return

        if event.id in self._seen:
            log.info("nats.duplicate_ignored", event_id=event.id, type=event.type)
            await msg.ack()
            return

        try:
            await handler(event)
        except Exception as exc:
            delivered = _delivery_count(msg)
            log.warning(
                "nats.handler_failed",
                event_id=event.id,
                delivered=delivered,
                detail=f"{type(exc).__name__}: {exc}"[:200],
            )
            if delivered >= self._max_deliveries:
                await self._dead_letter(event, reason=f"{type(exc).__name__}: {exc}"[:500])
                await msg.ack()
            else:
                # Not acked: JetStream redelivers after `ack_wait`.
                await msg.nak()
            return

        # Only now is it a duplicate if it comes back.
        self._seen.add(event.id)
        await msg.ack()

    # ----------------------------------------------------------------- dead letter

    async def _dead_letter(self, event: CloudEvent, *, reason: str) -> None:
        payload = event.model_dump()
        payload["dlq_reason"] = reason
        await self._js.publish(
            f"{DEAD_LETTER_SUBJECT}.{event.type.replace('.', '-')}",
            json.dumps(payload).encode(),
            headers={"Nats-Msg-Id": f"dlq-{event.id}"},
        )
        log.error("nats.dead_lettered", event_id=event.id, type=event.type)

    async def _dead_letter_raw(self, msg: Any, *, reason: str) -> None:
        await self._js.publish(
            f"{DEAD_LETTER_SUBJECT}.undecodable",
            json.dumps({"reason": reason, "subject": msg.subject}).encode(),
        )


def _durable_name(source: str, event_type: str, tenant_id: str) -> str:
    """A durable consumer name NATS accepts: no dots, no wildcards."""
    parts = [source, event_type.replace(".", "-"), tenant_id or "all"]
    return "-".join(p.replace(".", "-").replace("*", "all").replace(">", "all") for p in parts)


def _delivery_count(msg: Any) -> int:
    metadata = getattr(msg, "metadata", None)
    return int(getattr(metadata, "num_delivered", 1) or 1)


def _safe(url: str) -> str:
    """Never log credentials embedded in the broker URL."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    return f"{scheme}://***@{rest.rpartition('@')[2]}"
