"""Long-term memory: facts and preferences that outlive a session.

``LongTermMemory`` is a ``Protocol`` with two shipped implementations (ADR-002):

* ``KeyValueLongTermMemory`` -- the default. Lexical recall over a bounded per-scope set.
  Not a stub: it satisfies the whole contract, it is what unit tests exercise, and it is
  adequate for a development cell or a small area.
* ``Mem0LongTermMemory`` -- production. Delegates extraction, deduplication and
  contradiction handling to Mem0 over Qdrant.

Both are held to the same contract test, including the one that matters most:
``forget`` really deletes.

**Scope** decides who a fact is remembered for. ``area`` is what makes the cell "learn
from what the area's users do": a fact learned from one person becomes available to the
area -- which is exactly why nothing reaches this layer un-scrubbed.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from agent_forge.memory.scrubbing import Scrubber
from agent_forge.memory.store import KeyValueStore, dumps, loads, namespace
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

Scope = Literal["user", "area", "tenant"]

# Bound on the default adapter. Beyond this a cell should be on Mem0; keeping the list
# unbounded would turn a dev convenience into a silent performance cliff.
MAX_FACTS_PER_SCOPE = 500
_WORD = re.compile(r"[\wáéíóúüñ]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class MemoryFact:
    """One remembered thing."""

    text: str
    subject: str = ""  # user_id the fact came from, "" for area-level knowledge
    kind: str = "fact"  # fact | preference | procedure
    created_at: float = field(default_factory=time.time)
    source_task: str = ""

    @property
    def id(self) -> str:
        """Content-addressed, so remembering the same thing twice is idempotent."""
        return hashlib.sha256(f"{self.subject}|{self.text}".encode()).hexdigest()[:24]

    def to_payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "subject": self.subject,
            "kind": self.kind,
            "created_at": self.created_at,
            "source_task": self.source_task,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MemoryFact:
        return cls(
            text=str(payload.get("text", "")),
            subject=str(payload.get("subject", "")),
            kind=str(payload.get("kind", "fact")),
            created_at=float(payload.get("created_at", 0.0)),
            source_task=str(payload.get("source_task", "")),
        )


@runtime_checkable
class LongTermMemory(Protocol):
    """The contract every LTM adapter satisfies."""

    async def remember(
        self, tenant_id: str, facts: Sequence[MemoryFact], *, area: str = ""
    ) -> int: ...

    async def recall(
        self,
        tenant_id: str,
        query: str,
        *,
        area: str = "",
        user_id: str = "",
        limit: int = 5,
        kinds: Sequence[str] | None = None,
    ) -> list[MemoryFact]: ...

    async def forget(
        self, tenant_id: str, *, user_id: str | None = None, area: str = ""
    ) -> int: ...

    async def health(self) -> bool: ...


def scope_key(scope: Scope, *, area: str, user_id: str) -> str:
    """Which bucket a fact belongs to."""
    if scope == "user":
        return f"user:{user_id or 'anonymous'}"
    if scope == "area":
        return f"area:{area or 'default'}"
    return "tenant"


class KeyValueLongTermMemory:
    """Default adapter: lexical recall over the key-value store.

    Recall scores by term overlap with a recency tiebreaker. That is weaker than
    embeddings and entirely honest about it -- the point of this adapter is that the
    whole memory contract works with no vector database, so the tests that guarantee
    tenant isolation and deletion run everywhere.
    """

    def __init__(
        self,
        store: KeyValueStore,
        *,
        instance: str = "default",
        scope: Scope = "area",
        scrubber: Scrubber | None = None,
        retention_days: int = 365,
        max_facts: int = MAX_FACTS_PER_SCOPE,
    ) -> None:
        self._store = store
        self._instance = instance
        self._scope: Scope = scope
        self._scrubber = scrubber or Scrubber()
        self._retention = max(1, retention_days) * 86400
        self._max_facts = max_facts

    def _key(self, tenant_id: str, bucket: str) -> str:
        return namespace(tenant_id, self._instance, "ltm", bucket)

    async def _load(self, tenant_id: str, bucket: str) -> list[MemoryFact]:
        raw = await self._store.get(self._key(tenant_id, bucket))
        facts = [MemoryFact.from_payload(p) for p in loads(raw, [])]
        cutoff = time.time() - self._retention
        return [f for f in facts if f.created_at >= cutoff]

    async def _store_facts(self, tenant_id: str, bucket: str, facts: list[MemoryFact]) -> None:
        trimmed = sorted(facts, key=lambda f: f.created_at, reverse=True)[: self._max_facts]
        await self._store.set(
            self._key(tenant_id, bucket), dumps([f.to_payload() for f in trimmed])
        )

    async def remember(self, tenant_id: str, facts: Sequence[MemoryFact], *, area: str = "") -> int:
        """Scrub, deduplicate by content, and persist. Returns how many were new."""
        if not facts:
            return 0
        added = 0
        by_bucket: dict[str, list[MemoryFact]] = {}
        for fact in facts:
            scrubbed = self._scrubber.scrub(fact.text)
            clean = (
                fact
                if scrubbed.clean
                else MemoryFact(
                    text=scrubbed.text,
                    subject=fact.subject,
                    kind=fact.kind,
                    created_at=fact.created_at,
                    source_task=fact.source_task,
                )
            )
            bucket = scope_key(self._scope, area=area, user_id=fact.subject)
            by_bucket.setdefault(bucket, []).append(clean)

        for bucket, incoming in by_bucket.items():
            existing = await self._load(tenant_id, bucket)
            known = {f.id for f in existing}
            fresh = [f for f in incoming if f.id not in known]
            if not fresh:
                continue
            added += len(fresh)
            await self._store_facts(tenant_id, bucket, [*existing, *fresh])

        if added:
            log.info("ltm.remembered", tenant_id=tenant_id, facts=added, scope=self._scope)
        return added

    async def recall(
        self,
        tenant_id: str,
        query: str,
        *,
        area: str = "",
        user_id: str = "",
        limit: int = 5,
        kinds: Sequence[str] | None = None,
    ) -> list[MemoryFact]:
        """Best-matching facts for the query, across the buckets this scope can see.

        An empty ``query`` returns the most recent instead of the best match. That is how
        preferences are fetched: "answer briefly" is relevant to every turn and shares no
        vocabulary with any of them, so matching it against the question would never
        surface it.
        """
        buckets = _visible_buckets(self._scope, area=area, user_id=user_id)
        candidates: list[MemoryFact] = []
        for bucket in buckets:
            candidates.extend(await self._load(tenant_id, bucket))
        if kinds is not None:
            wanted = set(kinds)
            candidates = [f for f in candidates if f.kind in wanted]
        if not candidates:
            return []

        terms = set(_tokens(query))
        if not terms:
            return sorted(candidates, key=lambda f: f.created_at, reverse=True)[:limit]

        scored = [
            (len(terms & set(_tokens(fact.text))), fact.created_at, fact) for fact in candidates
        ]
        hits = [item for item in scored if item[0] > 0]
        hits.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [fact for _, _, fact in hits[:limit]]

    async def forget(self, tenant_id: str, *, user_id: str | None = None, area: str = "") -> int:
        """Delete. ``user_id=None`` wipes the whole tenant."""
        if user_id is None:
            keys = [
                key
                async for key in self._store.scan(namespace(tenant_id, self._instance, "ltm", "*"))
            ]
            removed = await self._store.delete(*keys) if keys else 0
            log.info("ltm.forgot_tenant", tenant_id=tenant_id, buckets=removed)
            return removed

        # A user's facts can sit in the user bucket and, under area scope, in the shared
        # one. Both have to go, or "forget me" quietly leaves half the record behind.
        removed = 0
        for bucket in _visible_buckets(self._scope, area=area, user_id=user_id):
            facts = await self._load(tenant_id, bucket)
            keep = [f for f in facts if f.subject != user_id]
            removed += len(facts) - len(keep)
            if len(keep) != len(facts):
                await self._store_facts(tenant_id, bucket, keep)
        log.info("ltm.forgot_user", tenant_id=tenant_id, facts=removed)
        return removed

    async def health(self) -> bool:
        return await self._store.health()


class Mem0LongTermMemory:
    """Production adapter over Mem0. Imported lazily (extra ``memory``, ADR-005)."""

    def __init__(
        self,
        *,
        instance: str = "default",
        scope: Scope = "area",
        scrubber: Scrubber | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        # Lazy: a cell on the default adapter must never import the extra.
        from mem0 import AsyncMemory

        self._client = AsyncMemory.from_config(config) if config else AsyncMemory()
        self._instance = instance
        self._scope: Scope = scope
        self._scrubber = scrubber or Scrubber()

    def _user(self, tenant_id: str, bucket: str) -> str:
        """Mem0's user_id is our namespace; the tenant is part of it, never a filter."""
        return f"{tenant_id}:{self._instance}:{bucket}"

    async def remember(self, tenant_id: str, facts: Sequence[MemoryFact], *, area: str = "") -> int:
        added = 0
        for fact in facts:
            text = self._scrubber.scrub(fact.text).text
            bucket = scope_key(self._scope, area=area, user_id=fact.subject)
            await self._client.add(
                messages=[{"role": "user", "content": text}],
                user_id=self._user(tenant_id, bucket),
                metadata={
                    "kind": fact.kind,
                    "subject": fact.subject,
                    "source_task": fact.source_task,
                },
            )
            added += 1
        return added

    async def recall(
        self,
        tenant_id: str,
        query: str,
        *,
        area: str = "",
        user_id: str = "",
        limit: int = 5,
        kinds: Sequence[str] | None = None,
    ) -> list[MemoryFact]:
        out: list[MemoryFact] = []
        for bucket in _visible_buckets(self._scope, area=area, user_id=user_id):
            found = await self._client.search(
                query=query, user_id=self._user(tenant_id, bucket), limit=limit
            )
            for item in found.get("results", []):
                metadata = item.get("metadata") or {}
                out.append(
                    MemoryFact(
                        text=str(item.get("memory", "")),
                        subject=str(metadata.get("subject", "")),
                        kind=str(metadata.get("kind", "fact")),
                        source_task=str(metadata.get("source_task", "")),
                    )
                )
        if kinds is not None:
            wanted = set(kinds)
            out = [f for f in out if f.kind in wanted]
        return out[:limit]

    async def forget(self, tenant_id: str, *, user_id: str | None = None, area: str = "") -> int:
        buckets = (
            _visible_buckets(self._scope, area=area, user_id=user_id or "")
            if user_id is not None
            else [scope_key(s, area=area, user_id="") for s in ("area", "tenant")]
        )
        removed = 0
        for bucket in buckets:
            await self._client.delete_all(user_id=self._user(tenant_id, bucket))
            removed += 1
        return removed

    async def health(self) -> bool:
        try:
            await self._client.search(query="health", user_id="health", limit=1)
        except Exception:
            return False
        return True


def _visible_buckets(scope: Scope, *, area: str, user_id: str) -> list[str]:
    """Buckets a query at this scope may read.

    Narrower scopes see less: ``user`` never reads the shared area bucket, because a fact
    learned from a colleague is not this user's memory.
    """
    if scope == "user":
        return [scope_key("user", area=area, user_id=user_id)]
    if scope == "area":
        return [
            scope_key("area", area=area, user_id=user_id),
            scope_key("user", area=area, user_id=user_id),
        ]
    return [
        "tenant",
        scope_key("area", area=area, user_id=user_id),
        scope_key("user", area=area, user_id=user_id),
    ]


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _WORD.findall(text) if len(t) > 2]
