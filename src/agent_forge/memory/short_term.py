"""Short-term memory: the session buffer.

Two behaviours that matter more than the storage:

* **Condense, do not truncate.** When the window grows past the limit the oldest turns
  are folded into a running summary and dropped. Cutting the conversation in half instead
  loses exactly the context the user assumes the agent still has.
* **The scratchpad is not memory.** Intermediate reasoning and tool output for the
  current turn live here and expire with the session; promoting them to long term is a
  separate, deliberate act (``episodic.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_forge.core.classification import Classification
from agent_forge.core.errors import ModelGatewayError
from agent_forge.core.state import Message
from agent_forge.gateway.litellm_client import GovernedGateway
from agent_forge.memory.scrubbing import Scrubber
from agent_forge.memory.store import KeyValueStore, dumps, loads, namespace
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

# Turns kept verbatim before the oldest start folding into the summary.
DEFAULT_WINDOW = 20
SUMMARY_TRIGGER_RATIO = 1.5


@dataclass(slots=True)
class Session:
    """What is remembered about one conversation."""

    thread_id: str
    messages: list[Message] = field(default_factory=list)
    summary: str = ""
    turns_summarised: int = 0

    def as_context(self) -> list[Message]:
        """Summary first, then the verbatim window."""
        if not self.summary:
            return list(self.messages)
        header = Message(
            role="system",
            content=f"Resumen de la conversacion previa:\n{self.summary}",
        )
        return [header, *self.messages]

    def to_payload(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "summary": self.summary,
            "turns_summarised": self.turns_summarised,
            "messages": [m.model_dump(mode="json") for m in self.messages],
        }

    @classmethod
    def from_payload(cls, thread_id: str, payload: dict[str, Any]) -> Session:
        return cls(
            thread_id=thread_id,
            summary=str(payload.get("summary", "")),
            turns_summarised=int(payload.get("turns_summarised", 0)),
            messages=[Message.model_validate(m) for m in payload.get("messages", [])],
        )


class ShortTermMemory:
    """Per-thread conversation buffer with TTL and incremental summarisation."""

    def __init__(
        self,
        store: KeyValueStore,
        *,
        instance: str = "default",
        ttl_minutes: int = 240,
        window: int = DEFAULT_WINDOW,
        scrubber: Scrubber | None = None,
        gateway: GovernedGateway | None = None,
        summary_model: str = "local/fast",
    ) -> None:
        self._store = store
        self._instance = instance
        self._ttl = max(1, ttl_minutes) * 60
        self._window = max(2, window)
        self._scrubber = scrubber or Scrubber()
        self._gateway = gateway
        self._summary_model = summary_model

    def _key(self, tenant_id: str, thread_id: str) -> str:
        return namespace(tenant_id, self._instance, "stm", thread_id)

    async def load(self, tenant_id: str, thread_id: str) -> Session:
        raw = await self._store.get(self._key(tenant_id, thread_id))
        payload = loads(raw, {})
        return Session.from_payload(thread_id, payload)

    async def save(self, tenant_id: str, session: Session) -> None:
        await self._store.set(
            self._key(tenant_id, session.thread_id),
            dumps(session.to_payload()),
            ttl_seconds=self._ttl,
        )

    async def append(
        self,
        tenant_id: str,
        thread_id: str,
        *messages: Message,
        classification: Classification = Classification.C0,
    ) -> Session:
        """Add turns, scrubbing them first, and condense when the window overflows."""
        session = await self.load(tenant_id, thread_id)
        for message in messages:
            scrubbed = self._scrubber.scrub(message.content)
            if not scrubbed.clean:
                log.info("stm.scrubbed", kinds=scrubbed.kinds(), thread_id=thread_id)
            session.messages.append(message.model_copy(update={"content": scrubbed.text}))

        if len(session.messages) > self._window * SUMMARY_TRIGGER_RATIO:
            await self._condense(session, classification=classification, tenant_id=tenant_id)

        await self.save(tenant_id, session)
        return session

    async def history(self, tenant_id: str, thread_id: str) -> list[Message]:
        """Context for the model: summary plus the verbatim window."""
        session = await self.load(tenant_id, thread_id)
        return session.as_context()

    async def clear(self, tenant_id: str, thread_id: str) -> int:
        return await self._store.delete(self._key(tenant_id, thread_id))

    async def forget_tenant(self, tenant_id: str) -> int:
        """Drop every session of a tenant. Part of the right-to-be-forgotten path."""
        keys = [
            key async for key in self._store.scan(namespace(tenant_id, self._instance, "stm", "*"))
        ]
        return await self._store.delete(*keys) if keys else 0

    # ------------------------------------------------------------ condensing

    async def _condense(
        self, session: Session, *, classification: Classification, tenant_id: str
    ) -> None:
        """Fold the oldest turns into the summary and drop them."""
        overflow = len(session.messages) - self._window
        if overflow <= 0:
            return
        older, session.messages = session.messages[:overflow], session.messages[overflow:]

        transcript = "\n".join(f"{m.role}: {m.content}" for m in older)
        summary = await self._summarise(session.summary, transcript, classification, tenant_id)
        session.summary = summary
        session.turns_summarised += len(older)
        log.info(
            "stm.condensed",
            thread_id=session.thread_id,
            folded=len(older),
            kept=len(session.messages),
        )

    async def _summarise(
        self,
        previous: str,
        transcript: str,
        classification: Classification,
        tenant_id: str,
    ) -> str:
        """Ask the fast model, falling back to a deterministic digest.

        The fallback matters: losing the model must degrade the summary, never lose the
        conversation. A truncated verbatim digest is worse than a good summary and far
        better than nothing.
        """
        if self._gateway is None:
            return _mechanical_summary(previous, transcript)
        try:
            response = await self._gateway.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "Resume la conversacion conservando hechos, decisiones y "
                            "datos concretos. Maximo 150 palabras. No inventes nada."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Resumen previo:\n{previous or '(ninguno)'}\n\n"
                            f"Turnos nuevos a integrar:\n{transcript}"
                        ),
                    },
                ],
                classification=classification,
                preferred=[self._summary_model],
                tenant_id=tenant_id,
                temperature=0.0,
                max_tokens=400,
            )
        except ModelGatewayError as exc:
            log.warning("stm.summary_unavailable", detail=str(exc))
            return _mechanical_summary(previous, transcript)
        return response.content.strip() or _mechanical_summary(previous, transcript)


def _mechanical_summary(previous: str, transcript: str, limit: int = 1200) -> str:
    """Deterministic digest used when no model is reachable."""
    merged = f"{previous}\n{transcript}".strip()
    if len(merged) <= limit:
        return merged
    return merged[-limit:].lstrip()
