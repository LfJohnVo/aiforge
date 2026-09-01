"""Agent memory: short term, long term, episodic and the semantic cache.

``MemoryManager`` is the single object the graph and the admin API talk to. It exists for
one reason beyond convenience: **forget has to reach every layer**. A right-to-be-
forgotten request that clears three of four stores is a compliance failure, and the only
way to keep that true as layers are added is to have one place that owns the sweep.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_forge.core.state import AgentState, Message
from agent_forge.memory.episodic import Episode, EpisodicMemory
from agent_forge.memory.long_term import (
    KeyValueLongTermMemory,
    LongTermMemory,
    Mem0LongTermMemory,
    MemoryFact,
    Scope,
)
from agent_forge.memory.scrubbing import Scrubber, ScrubResult
from agent_forge.memory.semantic_cache import CachedAnswer, CacheHit, SemanticCache
from agent_forge.memory.short_term import Session, ShortTermMemory
from agent_forge.memory.store import (
    InMemoryStore,
    KeyValueStore,
    MemoryStoreError,
    RedisStore,
    build_store,
    namespace,
)
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "CacheHit",
    "CachedAnswer",
    "Episode",
    "EpisodicMemory",
    "ForgetReport",
    "InMemoryStore",
    "KeyValueLongTermMemory",
    "KeyValueStore",
    "LongTermMemory",
    "Mem0LongTermMemory",
    "MemoryFact",
    "MemoryManager",
    "MemoryStoreError",
    "RedisStore",
    "Scope",
    "ScrubResult",
    "Scrubber",
    "SemanticCache",
    "Session",
    "ShortTermMemory",
    "build_store",
    "namespace",
]


@dataclass(frozen=True, slots=True)
class ForgetReport:
    """What a forget actually deleted, per layer. Goes to the ledger."""

    scope: str
    subject: str
    short_term: int = 0
    long_term: int = 0
    episodic: int = 0
    semantic_cache: int = 0

    @property
    def total(self) -> int:
        return self.short_term + self.long_term + self.episodic + self.semantic_cache

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "subject": self.subject,
            "deleted": {
                "short_term": self.short_term,
                "long_term": self.long_term,
                "episodic": self.episodic,
                "semantic_cache": self.semantic_cache,
            },
            "total": self.total,
        }


@dataclass(slots=True)
class MemoryManager:
    """The four memory layers, wired to one store and one scrubber."""

    store: KeyValueStore
    short_term: ShortTermMemory
    long_term: LongTermMemory
    episodic: EpisodicMemory
    cache: SemanticCache
    scrubber: Scrubber
    scope: Scope = "area"
    ltm_enabled: bool = True
    _instance: str = field(default="default")

    # ------------------------------------------------------------- retrieval

    async def context_for(
        self,
        state: AgentState,
        *,
        limit_facts: int = 5,
        limit_hints: int = 3,
    ) -> dict[str, Any]:
        """Everything memory can contribute to this turn.

        Bounded by the requester's ceiling: recalled facts and episodes are memory of the
        *area*, and a requester must not learn from what they could not have read.
        """
        question = state.last_user_message
        tenant = state.identity.tenant_id
        ceiling = state.identity.classification_ceiling

        history = await self.short_term.history(tenant, state.thread_id)
        facts: list[MemoryFact] = []
        if self.ltm_enabled:
            user = state.identity.user_id or ""
            if question:
                facts = await self.long_term.recall(
                    tenant,
                    question,
                    area=state.area,
                    user_id=user,
                    limit=limit_facts,
                    kinds=["fact"],
                )
            # Preferences are pulled unconditionally: "answer briefly" applies to every
            # turn and shares no vocabulary with any of them, so a query-driven recall
            # would never return it.
            preferences = await self.long_term.recall(
                tenant, "", area=state.area, user_id=user, limit=3, kinds=["preference"]
            )
            known = {f.id for f in facts}
            facts.extend(p for p in preferences if p.id not in known)
        hints = await self.episodic.hints(
            tenant, question, area=state.area, ceiling=ceiling, limit=limit_hints
        )
        return {"history": history, "facts": facts, "hints": hints}

    async def cached_answer(self, state: AgentState) -> CacheHit | None:
        return await self.cache.get(
            state.identity.tenant_id,
            state.last_user_message,
            area=state.area,
            ceiling=state.identity.classification_ceiling,
            groups=state.identity.groups,
        )

    # ------------------------------------------------------------- recording

    async def record_turn(
        self,
        state: AgentState,
        *,
        user_message: Message | None = None,
        assistant_message: Message | None = None,
    ) -> None:
        """Persist the turn in short-term memory."""
        messages = [m for m in (user_message, assistant_message) if m is not None]
        if messages:
            await self.short_term.append(
                state.identity.tenant_id,
                state.thread_id,
                *messages,
                classification=state.classification,
            )

    async def record_outcome(self, state: AgentState, *, feedback: str = "") -> None:
        """Learn from a finished task: cache the answer and distil an episode.

        Only completed tasks. Caching an answer the judge rejected would serve the bad
        answer to the next person who asks.
        """
        if state.status != "completed" or not state.answer:
            return

        await self.cache.put(
            state.identity.tenant_id,
            state.last_user_message,
            state.answer,
            area=state.area,
            classification=state.classification,
            groups=state.identity.groups,
            citations=[c.reference for c in state.citations],
        )
        await self.episodic.record(state, feedback=feedback)

    async def remember_facts(
        self, state: AgentState, facts: Sequence[str], *, kind: str = "fact"
    ) -> int:
        """Store durable facts learned from this interaction."""
        if not self.ltm_enabled or not facts:
            return 0
        subject = state.identity.user_id or ""
        return await self.long_term.remember(
            state.identity.tenant_id,
            [
                MemoryFact(text=text, subject=subject, kind=kind, source_task=state.task_id)
                for text in facts
                if text.strip()
            ],
            area=state.area,
        )

    # ---------------------------------------------------------------- forget

    async def forget(
        self,
        tenant_id: str,
        *,
        scope: str = "user",
        user_id: str | None = None,
        area: str = "",
        thread_ids: Sequence[str] = (),
    ) -> ForgetReport:
        """Delete across **every** layer. This is the compliance path.

        ``scope="tenant"`` wipes the tenant. ``scope="user"`` removes that person's facts
        and their sessions, and invalidates the cache for the area, because a cached
        answer may have been synthesised from what is being deleted.
        """
        if scope == "tenant":
            report = ForgetReport(
                scope="tenant",
                subject=tenant_id,
                short_term=await self.short_term.forget_tenant(tenant_id),
                long_term=await self.long_term.forget(tenant_id, user_id=None, area=area),
                episodic=await self.episodic.forget(tenant_id),
                semantic_cache=await self.cache.invalidate(tenant_id),
            )
        else:
            removed_sessions = 0
            for thread_id in thread_ids:
                removed_sessions += await self.short_term.clear(tenant_id, thread_id)
            report = ForgetReport(
                scope="user",
                subject=user_id or "",
                short_term=removed_sessions,
                long_term=await self.long_term.forget(tenant_id, user_id=user_id, area=area),
                episodic=0,
                semantic_cache=await self.cache.invalidate(tenant_id, area=area),
            )

        log.info("memory.forgotten", **report.to_dict())
        return report

    async def health(self) -> bool:
        return await self.store.health()

    def stats(self) -> dict[str, Any]:
        return {
            "cache_hits": self.cache.hits,
            "cache_misses": self.cache.misses,
            "cache_hit_rate": round(self.cache.hit_rate, 4),
            "ltm_enabled": self.ltm_enabled,
            "scope": self.scope,
        }


def build_memory(
    *,
    store: KeyValueStore | None = None,
    redis_url: str = "",
    redis_password: str = "",
    instance: str = "default",
    scope: Scope = "area",
    ltm_enabled: bool = True,
    episodic_enabled: bool = True,
    stm_ttl_minutes: int = 240,
    cache_enabled: bool = True,
    cache_similarity: float = 0.92,
    cache_ttl_hours: int = 72,
    retention_days: int = 365,
    scrubber: Scrubber | None = None,
    gateway: Any | None = None,
    fast_model: str = "local/fast",
    long_term: LongTermMemory | None = None,
) -> MemoryManager:
    """Assemble the memory stack from profile settings."""
    backing = store if store is not None else build_store(redis_url, password=redis_password)
    cleaner = scrubber or Scrubber()
    ltm = long_term or KeyValueLongTermMemory(
        backing,
        instance=instance,
        scope=scope,
        scrubber=cleaner,
        retention_days=retention_days,
    )
    return MemoryManager(
        store=backing,
        short_term=ShortTermMemory(
            backing,
            instance=instance,
            ttl_minutes=stm_ttl_minutes,
            scrubber=cleaner,
            gateway=gateway,
            summary_model=fast_model,
        ),
        long_term=ltm,
        episodic=EpisodicMemory(
            backing,
            instance=instance,
            enabled=episodic_enabled,
            scrubber=cleaner,
            retention_days=retention_days,
        ),
        cache=SemanticCache(
            backing,
            instance=instance,
            enabled=cache_enabled,
            similarity=cache_similarity,
            ttl_hours=cache_ttl_hours,
            scrubber=cleaner,
        ),
        scrubber=cleaner,
        scope=scope,
        ltm_enabled=ltm_enabled,
        _instance=instance,
    )
