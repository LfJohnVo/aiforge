"""Knowledge: ingestion, retrieval and the access control that governs both.

``KnowledgeService`` is what the graph sees. It exists so that there is exactly one place
that turns an identity into a retrieval, and so that adding a retrieval branch cannot
accidentally bypass the filter.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_forge.core.classification import Classification
from agent_forge.core.state import Identity
from agent_forge.knowledge.access_control import (
    AccessFilter,
    PolicyView,
    apply,
    build_filter,
    to_qdrant_filter,
)
from agent_forge.knowledge.cag import CagPreloader, StableCorpus
from agent_forge.knowledge.classifier import ClassificationResult, Classifier
from agent_forge.knowledge.documents import (
    AccessControl,
    Chunk,
    Document,
    RetrievalResult,
    SourceRef,
    build_chunks,
    chunk_text,
)
from agent_forge.knowledge.graphrag import GraphStore, build_graph_store
from agent_forge.knowledge.ingestion import IngestionPipeline, IngestionReport
from agent_forge.knowledge.rag import (
    Embeddings,
    GatewayReranker,
    HybridRetriever,
    RetrievalRequest,
    VectorStore,
    build_embeddings,
    build_vector_store,
)
from agent_forge.knowledge.sources import SourceReader, SyncCursor, build_reader
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "AccessControl",
    "AccessFilter",
    "CagPreloader",
    "Chunk",
    "ClassificationResult",
    "Classifier",
    "Document",
    "Embeddings",
    "GraphStore",
    "HybridRetriever",
    "IngestionPipeline",
    "IngestionReport",
    "KnowledgeService",
    "PolicyView",
    "RetrievalRequest",
    "RetrievalResult",
    "SourceReader",
    "SourceRef",
    "StableCorpus",
    "SyncCursor",
    "VectorStore",
    "apply",
    "build_chunks",
    "build_embeddings",
    "build_filter",
    "build_graph_store",
    "build_knowledge",
    "build_reader",
    "build_vector_store",
    "chunk_text",
    "to_qdrant_filter",
]


@dataclass(slots=True)
class KnowledgeService:
    """The knowledge layer as the graph consumes it."""

    retriever: HybridRetriever
    pipeline: IngestionPipeline
    cag: CagPreloader
    vector_store: VectorStore
    graph_store: GraphStore | None = None
    enabled: bool = True
    top_k: int = 8
    cursors: dict[str, SyncCursor] = field(default_factory=dict)

    async def retrieve(
        self,
        query: str,
        identity: Identity,
        *,
        policy: PolicyView | None = None,
        top_k: int | None = None,
    ) -> list[RetrievalResult]:
        """Everything this requester may see for this query."""
        if not self.enabled:
            return []
        return await self.retriever.retrieve(
            RetrievalRequest(
                query=query,
                identity=identity,
                top_k=top_k or self.top_k,
                policy=policy or PolicyView(),
            )
        )

    def retriever_for(self, identity: Identity, *, policy: PolicyView | None = None) -> Any:
        """A closure the domain subgraph calls with just a query.

        The subgraph never sees the identity, the filter or the store; it asks a question
        and receives what the requester is allowed to have. That is what keeps a domain
        plugin from being able to widen its own access.
        """

        async def retrieve(query: str) -> list[RetrievalResult]:
            return await self.retrieve(query, identity, policy=policy)

        return retrieve

    async def stable_corpus(
        self, identity: Identity, *, policy: PolicyView | None = None
    ) -> StableCorpus:
        return await self.cag.load(build_filter(identity, policy=policy or PolicyView()))

    async def sync(self, reader: SourceReader) -> IngestionReport:
        """Run one incremental sync, remembering where it got to."""
        cursor = self.cursors.setdefault(reader.kind, SyncCursor())
        return await self.pipeline.sync(reader, cursor)

    async def forget_tenant(self, tenant_id: str) -> int:
        return await self.pipeline.forget_tenant(tenant_id)

    async def stats(self, tenant_id: str) -> dict[str, Any]:
        return {
            **await self.pipeline.stats(tenant_id),
            "enabled": self.enabled,
            "top_k": self.top_k,
            "cag_enabled": self.cag.enabled,
            "last_retrieval": self.retriever.last_report.to_dict(),
        }

    async def health(self) -> bool:
        vector_ok = await self.vector_store.health()
        graph_ok = await self.graph_store.health() if self.graph_store else True
        return vector_ok and graph_ok

    async def aclose(self) -> None:
        await self.vector_store.aclose()
        if self.graph_store is not None:
            await self.graph_store.aclose()


def build_knowledge(
    *,
    profile: Any,
    gateway: Any | None = None,
    env: Mapping[str, str] | None = None,
    vector_store: VectorStore | None = None,
    graph_store: GraphStore | None = None,
    embeddings: Embeddings | None = None,
    vocabulary: Sequence[str] = (),
) -> KnowledgeService:
    """Assemble the knowledge layer from the profile."""
    source = dict(env or {})
    knowledge = profile.knowledge

    store = vector_store or build_vector_store(
        source.get("QDRANT_URL", ""),
        api_key=source.get("QDRANT_API_KEY", ""),
        strict=source.get("AGENT_FORGE_ENV", "development") == "production",
    )
    production = source.get("AGENT_FORGE_ENV", "development") == "production"
    # Which alias serves embeddings and reranking is deployment shape, not profile: the
    # same profile runs against vLLM on a GPU host and against Ollama on a laptop.
    embedder = embeddings or build_embeddings(
        gateway,
        alias=source.get("EMBEDDING_MODEL") or "local/embeddings",
        strict=production,
    )
    # No RERANK_MODEL means the lexical reranker, which is a deliberate default rather
    # than an omission: it needs no second model and keeps the retrieve-wide-then-narrow
    # shape identical in every environment.
    rerank_alias = source.get("RERANK_MODEL", "").strip()
    reranker = (
        GatewayReranker(gateway, alias=rerank_alias)
        if rerank_alias and gateway is not None
        else None
    )
    graph = (
        graph_store
        if graph_store is not None
        else build_graph_store(
            source.get("NEO4J_URI", ""),
            source.get("NEO4J_USER", "neo4j"),
            source.get("NEO4J_PASSWORD", ""),
            strict=source.get("AGENT_FORGE_ENV", "development") == "production",
            enabled=knowledge.graphrag.enabled,
        )
    )

    retriever = HybridRetriever(
        store,
        embedder,
        graph=graph,
        reranker=reranker,
        hybrid=knowledge.rag.hybrid,
        rerank=knowledge.rag.rerank,
    )
    classifier = Classifier(
        default=knowledge.default_classification,
        gateway=gateway,
        model_alias=profile.models.fast,
    )
    pipeline = IngestionPipeline(
        vector_store=store,
        embeddings=embedder,
        classifier=classifier,
        graph_store=graph,
        vocabulary=[*vocabulary, *profile.domain.intents_extra],
    )
    preloader = CagPreloader(
        store=store,
        patterns=tuple(knowledge.cag.stable_corpus),
        enabled=knowledge.cag.enabled,
    )

    log.info(
        "knowledge.ready",
        rag=knowledge.rag.enabled,
        graphrag=knowledge.graphrag.enabled,
        cag=knowledge.cag.enabled,
        default_classification=str(knowledge.default_classification),
    )
    return KnowledgeService(
        retriever=retriever,
        pipeline=pipeline,
        cag=preloader,
        vector_store=store,
        graph_store=graph,
        enabled=knowledge.rag.enabled,
        top_k=knowledge.rag.top_k,
    )


def highest_classification(results: Sequence[RetrievalResult]) -> Classification:
    """Highest classification among retrieved chunks; folded into the task maximum."""
    levels = [r.chunk.classification for r in results]
    return max(levels) if levels else Classification.C0
