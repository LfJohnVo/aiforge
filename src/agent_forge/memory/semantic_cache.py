"""Semantic cache: the operational half of CAG.

A repeated question in an area should not cost a model call. The saving is real -- most
of what an area asks its agent has been asked before.

**The part that is not an optimisation.** A cache entry records the classification
ceiling and the group set it was produced under, and only serves a request whose reach is
the same or narrower. Skipping that turns the cache into a channel that hands one user
another user's answer, which is a worse failure than any cache miss. That check is not
configurable.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from agent_forge.core.classification import Classification
from agent_forge.core.errors import ModelGatewayError
from agent_forge.gateway.litellm_client import GovernedGateway
from agent_forge.memory.scrubbing import Scrubber
from agent_forge.memory.store import KeyValueStore, dumps, loads, namespace
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

MAX_ENTRIES_PER_TENANT = 200
_WORD = re.compile(r"[\wáéíóúüñ]+", re.IGNORECASE)


@runtime_checkable
class Embedder(Protocol):
    """Turns text into a vector. Always a sovereign backend (see ``embed`` below)."""

    async def embed(self, text: str) -> list[float]: ...


class GatewayEmbedder:
    """Embeddings through the governed gateway, so C2+ text never leaves.

    Requests at C4 deliberately: the cache embeds the raw user question, whose
    sensitivity is not yet known at that point, so it must be treated as maximal. The
    policy then guarantees a sovereign backend.
    """

    def __init__(self, gateway: GovernedGateway, *, alias: str = "local/embeddings") -> None:
        self._gateway = gateway
        self._alias = alias

    async def embed(self, text: str) -> list[float]:
        vectors = await self._gateway.embed(
            [text], classification=Classification.C4, preferred=[self._alias]
        )
        return vectors[0] if vectors else []


@dataclass(frozen=True, slots=True)
class CachedAnswer:
    """A previous answer plus the reach it was produced under."""

    question: str
    answer: str
    citations: tuple[str, ...]
    classification: Classification
    groups: frozenset[str]
    created_at: float
    vector: tuple[float, ...] = ()

    def serves(self, ceiling: Classification, groups: frozenset[str]) -> bool:
        """Whether this entry may be shown to a requester with that reach.

        Two conditions, both necessary:
        * the answer's classification is within the requester's ceiling, and
        * the requester has every group the answer was built from.

        The second is the subtle one. An answer synthesised for someone in
        ``finanzas-lideres`` can contain material a plain ``finanzas`` member may not see,
        even when both are allowed the same classification level.
        """
        return self.classification <= ceiling and self.groups <= groups

    def to_payload(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "citations": list(self.citations),
            "classification": int(self.classification),
            "groups": sorted(self.groups),
            "created_at": self.created_at,
            "vector": list(self.vector),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CachedAnswer:
        return cls(
            question=str(payload.get("question", "")),
            answer=str(payload.get("answer", "")),
            citations=tuple(payload.get("citations", [])),
            classification=Classification(int(payload.get("classification", 0))),
            groups=frozenset(payload.get("groups", [])),
            created_at=float(payload.get("created_at", 0.0)),
            vector=tuple(payload.get("vector", [])),
        )


@dataclass(frozen=True, slots=True)
class CacheHit:
    entry: CachedAnswer
    similarity: float


class SemanticCache:
    """Similarity cache scoped by tenant, classification and groups."""

    def __init__(
        self,
        store: KeyValueStore,
        *,
        instance: str = "default",
        enabled: bool = True,
        similarity: float = 0.92,
        ttl_hours: int = 72,
        embedder: Embedder | None = None,
        max_entries: int = MAX_ENTRIES_PER_TENANT,
        scrubber: Scrubber | None = None,
    ) -> None:
        self._store = store
        self._instance = instance
        self._enabled = enabled
        self._threshold = similarity
        self._ttl = max(1, ttl_hours) * 3600
        self._embedder = embedder
        self._max_entries = max_entries
        self._scrubber = scrubber or Scrubber()
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def _key(self, tenant_id: str, area: str) -> str:
        return namespace(tenant_id, self._instance, "cache", area or "default")

    async def _entries(self, tenant_id: str, area: str) -> list[CachedAnswer]:
        raw = await self._store.get(self._key(tenant_id, area))
        cutoff = time.time() - self._ttl
        return [
            entry
            for entry in (CachedAnswer.from_payload(p) for p in loads(raw, []))
            if entry.created_at >= cutoff
        ]

    async def get(
        self,
        tenant_id: str,
        question: str,
        *,
        area: str = "",
        ceiling: Classification = Classification.C0,
        groups: Sequence[str] = (),
    ) -> CacheHit | None:
        """Best entry above the threshold that this requester is allowed to receive."""
        if not self._enabled or not question.strip():
            return None

        entries = await self._entries(tenant_id, area)
        if not entries:
            self.misses += 1
            return None

        reach = frozenset(groups)
        vector = await self._vector(question)
        best: CacheHit | None = None
        for entry in entries:
            if not entry.serves(ceiling, reach):
                continue
            score = self._similarity(question, vector, entry)
            if score >= self._threshold and (best is None or score > best.similarity):
                best = CacheHit(entry=entry, similarity=score)

        if best is None:
            self.misses += 1
            return None
        self.hits += 1
        log.info(
            "cache.hit",
            tenant_id=tenant_id,
            similarity=round(best.similarity, 3),
            classification=str(best.entry.classification),
        )
        return best

    async def put(
        self,
        tenant_id: str,
        question: str,
        answer: str,
        *,
        area: str = "",
        classification: Classification = Classification.C0,
        groups: Sequence[str] = (),
        citations: Sequence[str] = (),
    ) -> None:
        """Store an answer together with the reach it was produced under."""
        if not self._enabled or not question.strip() or not answer.strip():
            return

        # Answers containing PII are not cached at all, rather than cached redacted.
        # Redacting the answer would serve a degraded replay; keying on a redacted
        # question would let a different person's question match this entry. Skipping
        # is the only option that is both safe and honest -- and a PII-bearing answer is
        # specific to one person anyway, so it is a poor cache candidate.
        for label, text in (("question", question), ("answer", answer)):
            found = self._scrubber.scrub(text)
            if not found.clean:
                log.info("cache.skipped_pii", field=label, kinds=found.kinds())
                return

        entry = CachedAnswer(
            question=question,
            answer=answer,
            citations=tuple(citations),
            classification=classification,
            groups=frozenset(groups),
            created_at=time.time(),
            vector=tuple(await self._vector(question) or ()),
        )
        entries = await self._entries(tenant_id, area)
        # Replace an entry for the same question and reach rather than accumulating.
        entries = [
            e
            for e in entries
            if not (_normalise(e.question) == _normalise(question) and e.groups == entry.groups)
        ]
        entries.append(entry)
        entries.sort(key=lambda e: e.created_at, reverse=True)
        await self._store.set(
            self._key(tenant_id, area),
            dumps([e.to_payload() for e in entries[: self._max_entries]]),
            ttl_seconds=self._ttl,
        )

    async def invalidate(self, tenant_id: str, *, area: str = "") -> int:
        if area:
            return await self._store.delete(self._key(tenant_id, area))
        keys = [
            key
            async for key in self._store.scan(namespace(tenant_id, self._instance, "cache", "*"))
        ]
        return await self._store.delete(*keys) if keys else 0

    # ------------------------------------------------------------ similarity

    async def _vector(self, text: str) -> list[float] | None:
        if self._embedder is None:
            return None
        try:
            return await self._embedder.embed(text)
        except (ModelGatewayError, NotImplementedError) as exc:
            log.debug("cache.embedder_unavailable", detail=type(exc).__name__)
            return None

    def _similarity(self, question: str, vector: list[float] | None, entry: CachedAnswer) -> float:
        """Cosine when embeddings are available, lexical otherwise.

        The lexical fallback is not a pretend implementation: with the default 0.92
        threshold it matches near-identical phrasings, which is the bulk of real cache
        traffic in a single area. It simply misses paraphrases an embedding would catch.
        """
        if vector and entry.vector:
            return _cosine(vector, list(entry.vector))
        return _jaccard(question, entry.question)


def _normalise(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower().strip())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(_WORD.findall(stripped))


def _digest(text: str) -> str:
    return hashlib.sha256(_normalise(text).encode()).hexdigest()[:16]


def _jaccard(left: str, right: str) -> float:
    a, b = set(_normalise(left).split()), set(_normalise(right).split())
    if not a or not b:
        return 1.0 if a == b else 0.0
    return len(a & b) / len(a | b)


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(x * y for x, y in zip(left, right, strict=True))
    norm = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(y * y for y in right))
    return dot / norm if norm else 0.0
