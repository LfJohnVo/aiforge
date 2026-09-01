"""The ingestion pipeline: source -> parse -> chunk -> classify -> embed -> index.

Runs in the ingestion worker, not in the request path. Two properties worth stating:

* **A document is classified once, at ingestion**, and the label rides on every chunk into
  both stores. Classifying at query time would let the same document be labelled two ways
  on two requests, and the model-routing guarantee is built on that label.
* **A failed document does not fail the sync.** One unparseable PDF must not stop the
  other four hundred; it is counted, logged and reported.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_forge.core.classification import Classification
from agent_forge.knowledge.classifier import Classifier
from agent_forge.knowledge.documents import Chunk, Document, build_chunks
from agent_forge.knowledge.graphrag import GraphStore, extract_entities
from agent_forge.knowledge.rag.embeddings import Embeddings
from agent_forge.knowledge.rag.vector_store import VectorStore
from agent_forge.knowledge.sources import SourceReader, SyncCursor
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = ["IngestionPipeline", "IngestionReport"]

EMBED_BATCH = 32


@dataclass(slots=True)
class IngestionReport:
    """What a sync did. Returned by ``/admin/ingest`` and logged."""

    source_kind: str = ""
    documents: int = 0
    chunks: int = 0
    skipped: int = 0
    failed: int = 0
    by_classification: dict[str, int] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "documents": self.documents,
            "chunks": self.chunks,
            "skipped": self.skipped,
            "failed": self.failed,
            "by_classification": dict(sorted(self.by_classification.items())),
            "duration_ms": int(
                ((self.finished_at or datetime.now(UTC)) - self.started_at).total_seconds() * 1000
            ),
            "errors": self.errors[:10],
        }


class IngestionPipeline:
    """Turns a source into indexed, classified, access-controlled chunks."""

    def __init__(
        self,
        *,
        vector_store: VectorStore,
        embeddings: Embeddings,
        classifier: Classifier,
        graph_store: GraphStore | None = None,
        chunk_chars: int = 1200,
        overlap_chars: int = 150,
        vocabulary: Sequence[str] = (),
    ) -> None:
        self._store = vector_store
        self._embeddings = embeddings
        self._classifier = classifier
        self._graph = graph_store
        self._chunk_chars = chunk_chars
        self._overlap = overlap_chars
        self._vocabulary = list(vocabulary)

    async def sync(self, reader: SourceReader, cursor: SyncCursor) -> IngestionReport:
        """Ingest everything the reader reports as changed."""
        report = IngestionReport(source_kind=reader.kind)
        await self._store.ensure_collection(self._embeddings.dimension)

        async for document in reader.read(cursor):
            try:
                chunks = await self.ingest_document(document)
            except Exception as exc:
                # One bad document must not abort the sync.
                report.failed += 1
                report.errors.append(f"{document.source_id}: {type(exc).__name__}")
                log.warning(
                    "ingestion.document_failed",
                    source_id=document.source_id,
                    detail=type(exc).__name__,
                )
                continue
            if not chunks:
                report.skipped += 1
                continue
            report.documents += 1
            report.chunks += len(chunks)
            level = str(chunks[0].classification)
            report.by_classification[level] = report.by_classification.get(level, 0) + 1

        cursor.last_run = datetime.now(UTC)
        report.finished_at = datetime.now(UTC)
        log.info("ingestion.sync_complete", **report.to_dict())
        return report

    async def ingest_document(self, document: Document) -> list[Chunk]:
        """Classify, chunk, embed and index one document."""
        verdict = await self._classifier.classify(document)
        document.classification = verdict.classification
        log.debug(
            "ingestion.classified",
            source_id=document.source_id,
            classification=str(verdict.classification),
            method=verdict.method,
        )

        chunks = build_chunks(document, max_chars=self._chunk_chars, overlap=self._overlap)
        if not chunks:
            return []

        vectors = await self._embed_all([c.text for c in chunks])
        for chunk, vector in zip(chunks, vectors, strict=True):
            chunk.vector = tuple(vector)

        # Replace rather than accumulate: a re-ingested document that lost a section must
        # not leave the removed chunks retrievable.
        await self._store.delete_source(document.tenant_id, document.source_id)
        await self._store.upsert(chunks)

        if self._graph is not None:
            await self._index_graph(document, chunks)

        return chunks

    async def _embed_all(self, texts: Sequence[str]) -> list[list[float]]:
        batches = [texts[i : i + EMBED_BATCH] for i in range(0, len(texts), EMBED_BATCH)]
        results = await asyncio.gather(*(self._embeddings.embed(b) for b in batches))
        return [vector for batch in results for vector in batch]

    async def _index_graph(self, document: Document, chunks: Sequence[Chunk]) -> None:
        assert self._graph is not None
        await self._graph.delete_source(document.tenant_id, document.source_id)
        for chunk in chunks:
            entities, relations = extract_entities(chunk.text, vocabulary=self._vocabulary)
            if entities:
                await self._graph.upsert(document.tenant_id, chunk, entities, relations)

    async def forget_tenant(self, tenant_id: str) -> int:
        """Remove a tenant's whole corpus from both stores."""
        removed = await self._store.delete_tenant(tenant_id)
        if self._graph is not None:
            await self._graph.delete_tenant(tenant_id)
        return removed

    async def stats(self, tenant_id: str) -> dict[str, Any]:
        return {
            "chunks": await self._store.count(tenant_id),
            "graph_enabled": self._graph is not None,
        }


def default_classification_counts(chunks: Sequence[Chunk]) -> dict[str, int]:
    """Distribution of classifications, for the ingestion report and dashboards."""
    counts: dict[str, int] = {}
    for chunk in chunks:
        key = str(chunk.classification)
        counts[key] = counts.get(key, 0) + 1
    return counts


def highest_classification(chunks: Sequence[Chunk]) -> Classification:
    return max((c.classification for c in chunks), default=Classification.C0)
