"""Episodic memory: what worked, so the area's agent gets better at the area's work.

An episode records the *shape* of a successful interaction -- the question, the path
taken, the tools that helped, the feedback -- and is recalled later as a few-shot hint.

Two rules that keep this from becoming a leak:

* **Only successful, approved interactions are distilled.** Recording a rejected or
  escalated answer teaches the agent to repeat it.
* **An episode stores the pattern, not the payload.** The question is scrubbed and
  generalised; concrete identifiers become placeholders. What is worth remembering is
  "invoice questions are answered from the policy document", not the invoice number.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agent_forge.core.classification import Classification
from agent_forge.core.state import AgentState
from agent_forge.memory.scrubbing import Scrubber
from agent_forge.memory.store import KeyValueStore, dumps, loads, namespace
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

MAX_EPISODES = 300
_WORD = re.compile(r"[\wáéíóúüñ]+", re.IGNORECASE)
# Identifiers that vary per case and must not become part of the remembered pattern.
_IDENTIFIER = re.compile(
    r"\b(?:[A-Z]{2,5}-\d{2,}|#\d+|\d{4,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})\b"
)


@dataclass(frozen=True, slots=True)
class Episode:
    """One distilled interaction."""

    question_pattern: str
    intent: str
    tools_used: tuple[str, ...]
    citations_used: int
    outcome: str  # completed | approved
    classification: Classification
    feedback: str = ""
    created_at: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "question_pattern": self.question_pattern,
            "intent": self.intent,
            "tools_used": list(self.tools_used),
            "citations_used": self.citations_used,
            "outcome": self.outcome,
            "classification": int(self.classification),
            "feedback": self.feedback,
            "created_at": self.created_at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Episode:
        return cls(
            question_pattern=str(payload.get("question_pattern", "")),
            intent=str(payload.get("intent", "")),
            tools_used=tuple(payload.get("tools_used", [])),
            citations_used=int(payload.get("citations_used", 0)),
            outcome=str(payload.get("outcome", "completed")),
            classification=Classification(int(payload.get("classification", 0))),
            feedback=str(payload.get("feedback", "")),
            created_at=float(payload.get("created_at", 0.0)),
        )

    def as_hint(self) -> str:
        """One line the planner or subgraph can use as a few-shot cue."""
        tools = ", ".join(self.tools_used) if self.tools_used else "sin herramientas"
        cited = "con citas" if self.citations_used else "sin citas"
        return f"[{self.intent}] {self.question_pattern} -> {tools}, {cited}"


class EpisodicMemory:
    """Distils finished tasks into recallable patterns, per area."""

    def __init__(
        self,
        store: KeyValueStore,
        *,
        instance: str = "default",
        enabled: bool = True,
        scrubber: Scrubber | None = None,
        retention_days: int = 365,
        max_episodes: int = MAX_EPISODES,
    ) -> None:
        self._store = store
        self._instance = instance
        self._enabled = enabled
        self._scrubber = scrubber or Scrubber()
        self._retention = max(1, retention_days) * 86400
        self._max = max_episodes

    def _key(self, tenant_id: str, area: str) -> str:
        return namespace(tenant_id, self._instance, "episodes", area or "default")

    async def _load(self, tenant_id: str, area: str) -> list[Episode]:
        raw = await self._store.get(self._key(tenant_id, area))
        cutoff = time.time() - self._retention
        return [
            e for e in (Episode.from_payload(p) for p in loads(raw, [])) if e.created_at >= cutoff
        ]

    def distil(self, state: AgentState, *, feedback: str = "") -> Episode | None:
        """Turn a finished task into an episode, or None when it should not be learned."""
        if state.status != "completed" or state.verdict == "retry":
            return None
        question = state.last_user_message.strip()
        if not question:
            return None

        scrubbed = self._scrubber.scrub(question).text
        pattern = _generalise(scrubbed)
        return Episode(
            question_pattern=pattern,
            intent=str(state.scratchpad.get("intent", "question")),
            tools_used=tuple(sorted({t.tool for t in state.tool_calls if t.ok})),
            citations_used=len(state.citations),
            outcome="completed",
            classification=state.classification,
            feedback=self._scrubber.scrub(feedback).text if feedback else "",
            created_at=time.time(),
        )

    async def record(self, state: AgentState, *, feedback: str = "") -> Episode | None:
        """Distil and persist. Returns the episode when one was learned."""
        if not self._enabled:
            return None
        episode = self.distil(state, feedback=feedback)
        if episode is None:
            return None

        area = state.area
        episodes = await self._load(state.identity.tenant_id, area)
        # Same pattern and intent: keep the newest rather than accumulating duplicates.
        episodes = [
            e
            for e in episodes
            if not (e.question_pattern == episode.question_pattern and e.intent == episode.intent)
        ]
        episodes.append(episode)
        episodes.sort(key=lambda e: e.created_at, reverse=True)
        await self._store.set(
            self._key(state.identity.tenant_id, area),
            dumps([e.to_payload() for e in episodes[: self._max]]),
        )
        log.info("episodic.recorded", intent=episode.intent, tools=len(episode.tools_used))
        return episode

    async def recall(
        self,
        tenant_id: str,
        question: str,
        *,
        area: str = "",
        ceiling: Classification = Classification.C0,
        limit: int = 3,
    ) -> list[Episode]:
        """Similar past episodes, never above the requester's ceiling.

        The ceiling check matters: an episode records which tools worked for a kind of
        question, and that itself can be sensitive.
        """
        if not self._enabled:
            return []
        episodes = [e for e in await self._load(tenant_id, area) if e.classification <= ceiling]
        if not episodes:
            return []

        terms = set(_tokens(_generalise(question)))
        if not terms:
            return episodes[:limit]
        scored = [
            (len(terms & set(_tokens(e.question_pattern))), e.created_at, e) for e in episodes
        ]
        hits = [item for item in scored if item[0] > 0]
        hits.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [episode for _, _, episode in hits[:limit]]

    async def hints(
        self,
        tenant_id: str,
        question: str,
        *,
        area: str = "",
        ceiling: Classification = Classification.C0,
        limit: int = 3,
    ) -> list[str]:
        episodes = await self.recall(tenant_id, question, area=area, ceiling=ceiling, limit=limit)
        return [e.as_hint() for e in episodes]

    async def forget(self, tenant_id: str, *, area: str = "") -> int:
        if area:
            return await self._store.delete(self._key(tenant_id, area))
        keys = [
            key
            async for key in self._store.scan(namespace(tenant_id, self._instance, "episodes", "*"))
        ]
        return await self._store.delete(*keys) if keys else 0


def _generalise(text: str) -> str:
    """Replace case-specific identifiers so the pattern outlives the case."""
    return _IDENTIFIER.sub("<id>", text)[:300]


def _tokens(text: str) -> Sequence[str]:
    return [t.lower() for t in _WORD.findall(text) if len(t) > 2]
