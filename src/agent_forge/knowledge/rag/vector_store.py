"""Vector storage, with the access filter pushed into the query.

``InMemoryVectorStore`` and ``QdrantVectorStore`` implement the same ``VectorStore``
Protocol and are held to the same contract test. Both apply ``AccessFilter`` **before**
ranking and truncation -- the in-memory one by predicate, Qdrant by a native payload
filter evaluated inside the HNSW search (ADR-004).

The distinction is not academic. Filter after the search and a forbidden document
consumes a `top_k` slot, so the same question returns fewer results for one person than
another. That difference is the leak.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from agent_forge.core.errors import AgentForgeError
from agent_forge.knowledge.access_control import (
    INDEXED_PAYLOAD_FIELDS,
    AccessFilter,
    to_qdrant_filter,
)
from agent_forge.knowledge.documents import Chunk, RetrievalResult
from agent_forge.knowledge.rag.embeddings import cosine
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

DEFAULT_COLLECTION = "agent_knowledge"
REPO_COLLECTION = "repo_knowledge"


class VectorStoreError(AgentForgeError):
    """The vector store is unreachable or rejected an operation."""

    code = "vector_store_error"


@runtime_checkable
class VectorStore(Protocol):
    """What the retrieval layer needs from a vector database."""

    async def ensure_collection(self, dimension: int) -> None: ...

    async def upsert(self, chunks: Sequence[Chunk]) -> int: ...

    async def search(
        self, vector: Sequence[float], access: AccessFilter, *, limit: int = 8
    ) -> list[RetrievalResult]: ...

    async def scroll(self, access: AccessFilter, *, limit: int = 1000) -> list[Chunk]: ...

    async def delete_source(self, tenant_id: str, source_id: str) -> int: ...

    async def delete_tenant(self, tenant_id: str) -> int: ...

    async def count(self, tenant_id: str) -> int: ...

    async def health(self) -> bool: ...

    async def aclose(self) -> None: ...


class InMemoryVectorStore:
    """Exact-search store for development and tests.

    Brute-force cosine over every chunk. That is O(n) and entirely adequate for a dev
    corpus; the point of this class is that the ACL contract can be tested without
    Qdrant, not that it scales.
    """

    def __init__(self, collection: str = DEFAULT_COLLECTION) -> None:
        self.collection = collection
        self._chunks: dict[str, Chunk] = {}
        self._dimension = 0

    async def ensure_collection(self, dimension: int) -> None:
        self._dimension = dimension

    async def upsert(self, chunks: Sequence[Chunk]) -> int:
        for chunk in chunks:
            self._chunks[chunk.point_id] = chunk
        return len(chunks)

    async def search(
        self, vector: Sequence[float], access: AccessFilter, *, limit: int = 8
    ) -> list[RetrievalResult]:
        # Filter first, then rank, then truncate. Reordering these is the bug.
        visible = [c for c in self._chunks.values() if access.permits(c)]
        scored = [
            RetrievalResult(chunk=chunk, score=cosine(vector, chunk.vector), retriever="vector")
            for chunk in visible
            if chunk.vector
        ]
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:limit]

    async def scroll(self, access: AccessFilter, *, limit: int = 1000) -> list[Chunk]:
        return [c for c in self._chunks.values() if access.permits(c)][:limit]

    async def delete_source(self, tenant_id: str, source_id: str) -> int:
        doomed = [
            pid
            for pid, c in self._chunks.items()
            if c.tenant_id == tenant_id and c.source_id == source_id
        ]
        for pid in doomed:
            del self._chunks[pid]
        return len(doomed)

    async def delete_tenant(self, tenant_id: str) -> int:
        doomed = [pid for pid, c in self._chunks.items() if c.tenant_id == tenant_id]
        for pid in doomed:
            del self._chunks[pid]
        return len(doomed)

    async def count(self, tenant_id: str) -> int:
        return sum(1 for c in self._chunks.values() if c.tenant_id == tenant_id)

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        self._chunks.clear()


class QdrantVectorStore:
    """Qdrant adapter. The production store.

    Payload fields used by the filter are indexed on collection creation; without those
    indexes Qdrant falls back to a full scan and the filter stops being cheap.
    """

    def __init__(
        self,
        url: str,
        *,
        api_key: str = "",
        collection: str = DEFAULT_COLLECTION,
        timeout: int = 30,
    ) -> None:
        try:
            from qdrant_client import AsyncQdrantClient
        except ImportError as exc:  # pragma: no cover - guarded by capability check
            raise VectorStoreError(
                "the `knowledge` extra is required for the Qdrant store",
                hint="uv sync --extra knowledge",
            ) from exc
        self._client: Any = AsyncQdrantClient(url=url, api_key=api_key or None, timeout=timeout)
        self.collection = collection
        self._ready = False

    async def ensure_collection(self, dimension: int) -> None:
        from qdrant_client import models

        if self._ready:
            return
        try:
            exists = await self._client.collection_exists(self.collection)
            if not exists:
                await self._client.create_collection(
                    collection_name=self.collection,
                    vectors_config=models.VectorParams(
                        size=dimension, distance=models.Distance.COSINE
                    ),
                )
            for field, kind in INDEXED_PAYLOAD_FIELDS.items():
                await self._client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field,
                    field_schema=kind,
                    wait=True,
                )
        except Exception as exc:
            raise VectorStoreError(
                "could not prepare the Qdrant collection",
                collection=self.collection,
                detail=str(exc),
            ) from exc
        self._ready = True
        log.info("qdrant.ready", collection=self.collection, dimension=dimension)

    async def upsert(self, chunks: Sequence[Chunk]) -> int:
        from qdrant_client import models

        if not chunks:
            return 0
        points = [
            models.PointStruct(
                id=chunk.point_id, vector=list(chunk.vector), payload=chunk.to_payload()
            )
            for chunk in chunks
        ]
        try:
            await self._client.upsert(collection_name=self.collection, points=points, wait=True)
        except Exception as exc:
            raise VectorStoreError("qdrant upsert failed", detail=str(exc)) from exc
        return len(points)

    async def search(
        self, vector: Sequence[float], access: AccessFilter, *, limit: int = 8
    ) -> list[RetrievalResult]:
        from qdrant_client import models

        query_filter = models.Filter(**to_qdrant_filter(access))
        try:
            response = await self._client.query_points(
                collection_name=self.collection,
                query=list(vector),
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
        except Exception as exc:
            raise VectorStoreError("qdrant search failed", detail=str(exc)) from exc

        return [
            RetrievalResult(
                chunk=Chunk.from_payload(dict(point.payload or {})),
                score=float(point.score),
                retriever="vector",
            )
            for point in response.points
        ]

    async def scroll(self, access: AccessFilter, *, limit: int = 1000) -> list[Chunk]:
        from qdrant_client import models

        try:
            points, _ = await self._client.scroll(
                collection_name=self.collection,
                scroll_filter=models.Filter(**to_qdrant_filter(access)),
                limit=limit,
                with_payload=True,
            )
        except Exception as exc:
            raise VectorStoreError("qdrant scroll failed", detail=str(exc)) from exc
        return [Chunk.from_payload(dict(p.payload or {})) for p in points]

    async def delete_source(self, tenant_id: str, source_id: str) -> int:
        from qdrant_client import models

        count = await self._count_matching(tenant_id, source_id)
        await self._client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="tenant_id", match=models.MatchValue(value=tenant_id)
                        ),
                        models.FieldCondition(
                            key="source_id", match=models.MatchValue(value=source_id)
                        ),
                    ]
                )
            ),
            wait=True,
        )
        return count

    async def delete_tenant(self, tenant_id: str) -> int:
        from qdrant_client import models

        count = await self.count(tenant_id)
        await self._client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="tenant_id", match=models.MatchValue(value=tenant_id)
                        )
                    ]
                )
            ),
            wait=True,
        )
        return count

    async def count(self, tenant_id: str) -> int:
        return await self._count_matching(tenant_id, None)

    async def _count_matching(self, tenant_id: str, source_id: str | None) -> int:
        from qdrant_client import models

        must = [models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id))]
        if source_id is not None:
            must.append(
                models.FieldCondition(key="source_id", match=models.MatchValue(value=source_id))
            )
        try:
            result = await self._client.count(
                collection_name=self.collection,
                count_filter=models.Filter(must=must),
                exact=True,
            )
        except Exception as exc:
            raise VectorStoreError("qdrant count failed", detail=str(exc)) from exc
        return int(result.count)

    async def health(self) -> bool:
        try:
            await self._client.get_collections()
        except Exception:
            return False
        return True

    async def aclose(self) -> None:
        await self._client.close()


def build_vector_store(
    url: str = "", *, api_key: str = "", collection: str = DEFAULT_COLLECTION
) -> VectorStore:
    """Qdrant when configured, in-memory otherwise -- and say which."""
    if url:
        return QdrantVectorStore(url, api_key=api_key, collection=collection)
    log.warning(
        "vector_store.in_memory_selected",
        detail="QDRANT_URL is not set; the corpus is lost on restart",
    )
    return InMemoryVectorStore(collection)
