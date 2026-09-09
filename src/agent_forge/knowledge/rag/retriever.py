"""Hybrid retrieval: BM25 + vectors + graph, fused with RRF, then reranked.

The ordering rule that runs through this whole module: **the access filter is applied in
every branch, before fusion.** Fusing first and filtering after would reintroduce exactly
the side channel that ADR-004 exists to close -- the fused list length would depend on
what the requester was not allowed to see.

Reciprocal Rank Fusion is used rather than score normalisation because BM25 scores and
cosine similarities are not comparable quantities, and any attempt to make them
comparable ends up tuned to one corpus.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from agent_forge.core.classification import Classification
from agent_forge.core.errors import ModelGatewayError, SovereigntyError
from agent_forge.core.state import Identity
from agent_forge.gateway.litellm_client import GovernedGateway
from agent_forge.knowledge.access_control import AccessFilter, PolicyView, build_filter
from agent_forge.knowledge.documents import Chunk, RetrievalResult
from agent_forge.knowledge.rag.embeddings import Embeddings
from agent_forge.knowledge.rag.vector_store import VectorStore
from agent_forge.observability.logging import get_logger
from agent_forge.observability.metrics import get_metrics

log = get_logger(__name__)

# RRF constant. 60 is the value from the original paper and behaves well when the
# component rankers disagree, which is the case we care about.
RRF_K = 60
# Minimum reranker relevance (query-term coverage, 0..1) for a chunk to be returned.
# Without a floor, nearest-neighbour search always returns *something*: ask an area agent
# for a paella recipe and it hands back the travel policy, which the model may then cite.
# A citation to an irrelevant document is worse than no citation.
MIN_RELEVANCE = 0.15
_WORD = re.compile(r"[a-z0-9ñ]+")
# Folded, accent-free stopwords for ES and EN. Kept explicit rather than pulled from
# a corpus package: the list is short, reviewable, and one less dependency.
_STOPWORDS = frozenset(
    [
        "a",
        "al",
        "algo",
        "alguna",
        "algunas",
        "alguno",
        "algunos",
        "an",
        "and",
        "ante",
        "antes",
        "are",
        "as",
        "at",
        "be",
        "by",
        "como",
        "con",
        "contra",
        "cual",
        "cuando",
        "de",
        "del",
        "desde",
        "donde",
        "dos",
        "el",
        "ella",
        "ellas",
        "ello",
        "ellos",
        "en",
        "entre",
        "era",
        "eran",
        "eres",
        "es",
        "esa",
        "esas",
        "ese",
        "eso",
        "esos",
        "esta",
        "estaba",
        "estado",
        "estan",
        "estas",
        "este",
        "esto",
        "estos",
        "for",
        "from",
        "ha",
        "hace",
        "hacia",
        "han",
        "has",
        "hasta",
        "have",
        "hay",
        "how",
        "in",
        "is",
        "it",
        "its",
        "la",
        "las",
        "le",
        "les",
        "lo",
        "los",
        "mas",
        "me",
        "mi",
        "mis",
        "mucho",
        "muy",
        "no",
        "nos",
        "nosotros",
        "o",
        "of",
        "on",
        "or",
        "os",
        "otra",
        "otras",
        "otro",
        "otros",
        "para",
        "pero",
        "poco",
        "por",
        "porque",
        "que",
        "quien",
        "se",
        "sea",
        "segun",
        "ser",
        "si",
        "sin",
        "sobre",
        "solo",
        "son",
        "su",
        "sus",
        "tambien",
        "tanto",
        "te",
        "that",
        "the",
        "this",
        "tiene",
        "tienen",
        "to",
        "todo",
        "todos",
        "tu",
        "tus",
        "un",
        "una",
        "uno",
        "unos",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "y",
        "ya",
    ]
)


def tokenize(text: str) -> list[str]:
    """Fold accents, lowercase, drop stopwords. Spanish needs the accent folding."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(c for c in decomposed if not unicodedata.combining(c))
    return [t for t in _WORD.findall(folded) if t not in _STOPWORDS and len(t) > 1]


# --------------------------------------------------------------------------- BM25


class BM25Index:
    """Okapi BM25 over the chunks a requester may see.

    Built per query from the filtered candidate set rather than maintained globally: a
    global index would have to be filtered afterwards, and the corpus of one area is
    small enough that rebuilding is cheaper than the machinery to avoid it.
    """

    def __init__(self, chunks: Sequence[Chunk], *, k1: float = 1.5, b: float = 0.75) -> None:
        self._chunks = list(chunks)
        self._k1 = k1
        self._b = b
        self._docs = [tokenize(c.text) for c in self._chunks]
        self._lengths = [len(d) for d in self._docs]
        self._avg_len = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        self._df: dict[str, int] = {}
        for doc in self._docs:
            for term in set(doc):
                self._df[term] = self._df.get(term, 0) + 1
        self._n = len(self._docs)

    def search(self, query: str, *, limit: int = 20) -> list[RetrievalResult]:
        terms = tokenize(query)
        if not terms or not self._n:
            return []

        scores = [0.0] * self._n
        for term in terms:
            df = self._df.get(term)
            if not df:
                continue
            idf = math.log(1 + (self._n - df + 0.5) / (df + 0.5))
            for index, doc in enumerate(self._docs):
                freq = doc.count(term)
                if not freq:
                    continue
                length_norm = (
                    1
                    - self._b
                    + self._b * (self._lengths[index] / self._avg_len if self._avg_len else 1.0)
                )
                scores[index] += idf * (freq * (self._k1 + 1)) / (freq + self._k1 * length_norm)

        ranked = [
            RetrievalResult(chunk=self._chunks[i], score=score, retriever="bm25")
            for i, score in enumerate(scores)
            if score > 0
        ]
        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked[:limit]


# ------------------------------------------------------------------ graph + rerank


@runtime_checkable
class GraphRetriever(Protocol):
    """The GraphRAG side. Implemented in ``knowledge/graphrag``."""

    async def search(
        self, query: str, access: AccessFilter, *, limit: int = 10
    ) -> list[RetrievalResult]: ...


@runtime_checkable
class Reranker(Protocol):
    """Cross-encoder reranking of a fused candidate list."""

    async def rerank(
        self, query: str, results: Sequence[RetrievalResult], *, limit: int
    ) -> list[RetrievalResult]: ...


class GatewayReranker:
    """Cross-encoder reranking through the model gateway.

    Better than `LexicalReranker` because a cross-encoder reads the query and the chunk
    together, so it scores "does this passage answer this question" rather than "do these
    share words". It costs a model call over every candidate, which is why it reranks a
    shortlist and not the corpus.

    It degrades rather than fails: a reranker that is down should cost result *quality*,
    not the answer. The fused RRF order is already a reasonable ranking, so that is what
    the caller gets, and the event says so.
    """

    def __init__(
        self,
        gateway: GovernedGateway,
        *,
        alias: str = "local/rerank",
        fallback: Reranker | None = None,
    ) -> None:
        self._gateway = gateway
        self._alias = alias
        self._fallback = fallback or LexicalReranker()

    async def rerank(
        self, query: str, results: Sequence[RetrievalResult], *, limit: int
    ) -> list[RetrievalResult]:
        if not results:
            return []
        # The candidates carry the classification the answer will inherit; routing on the
        # accumulated maximum is what keeps a C3 chunk away from an external reranker.
        classification = max((r.chunk.classification for r in results), default=Classification.C0)
        try:
            scores = await self._gateway.rerank(
                query,
                [r.chunk.text for r in results],
                classification=classification,
                preferred=(self._alias,),
            )
        except (ModelGatewayError, SovereigntyError) as exc:
            log.error(
                "rerank.degraded_to_lexical",
                detail=str(exc),
                impact="results keep their fused order; relevance is weaker than configured",
            )
            get_metrics().degraded.labels(component="rerank").inc()
            return await self._fallback.rerank(query, results, limit=limit)

        rescored = [
            RetrievalResult(chunk=result.chunk, score=score, retriever="reranked")
            for result, score in zip(results, scores, strict=False)
        ]
        rescored.sort(key=lambda r: r.score, reverse=True)
        return rescored[:limit]


class LexicalReranker:
    """Term-coverage reranker, used when no cross-encoder is available.

    Scores by how much of the query a chunk actually covers, with a small bonus for
    proximity of the matched terms. Weaker than bge-reranker-v2-m3 and it says so; the
    value is that the pipeline shape -- retrieve wide, rerank down -- is the same in
    every environment, so the behaviour under test matches the behaviour in production.
    """

    async def rerank(
        self, query: str, results: Sequence[RetrievalResult], *, limit: int
    ) -> list[RetrievalResult]:
        terms = tokenize(query)
        if not terms:
            return list(results)[:limit]
        wanted = set(terms)

        rescored: list[RetrievalResult] = []
        for result in results:
            # A chunk that tokenizes to nothing -- all stopwords, a table of symbols, a
            # script this tokenizer does not split -- scores zero and sinks. It is not
            # dropped: a reranker reorders candidates, and one that silently removes
            # them turns a retrieval that found something into an answer with no source.
            tokens = tokenize(result.chunk.text)
            present = wanted & set(tokens)
            coverage = len(present) / len(wanted)
            rescored.append(
                RetrievalResult(
                    chunk=result.chunk,
                    score=coverage + 0.25 * _proximity(tokens, present),
                    retriever="reranked",
                )
            )
        rescored.sort(key=lambda r: r.score, reverse=True)
        return rescored[:limit]


def _proximity(tokens: Sequence[str], wanted: set[str]) -> float:
    """1.0 when every matched term sits in one window, decaying as they spread out."""
    if len(wanted) < 2:
        return 1.0 if wanted else 0.0
    positions = [i for i, t in enumerate(tokens) if t in wanted]
    if len(positions) < 2:
        return 0.0
    span = positions[-1] - positions[0] + 1
    return min(1.0, len(wanted) / span)


# ------------------------------------------------------------------------ fusion


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[RetrievalResult]], *, k: int = RRF_K
) -> list[RetrievalResult]:
    """Combine rankings by rank, not by score.

    BM25 scores and cosine similarities live on different scales; normalising them into
    comparability is a tuning exercise that never generalises. Rank does generalise.
    """
    fused: dict[str, tuple[float, RetrievalResult]] = {}
    for ranking in rankings:
        for position, result in enumerate(ranking, start=1):
            key = result.chunk.point_id
            contribution = 1.0 / (k + position)
            previous = fused.get(key)
            total = (previous[0] if previous else 0.0) + contribution
            fused[key] = (total, previous[1] if previous else result)

    ordered = sorted(fused.values(), key=lambda item: item[0], reverse=True)
    return [
        RetrievalResult(chunk=result.chunk, score=score, retriever="fused")
        for score, result in ordered
    ]


# --------------------------------------------------------------------- retriever


@dataclass(slots=True)
class RetrievalRequest:
    """One retrieval, with the identity it is performed on behalf of."""

    query: str
    identity: Identity
    top_k: int = 8
    policy: PolicyView = field(default_factory=PolicyView)


@dataclass(frozen=True, slots=True)
class RetrievalReport:
    """What each branch contributed. Goes to the trace, never to the user."""

    vector: int = 0
    bm25: int = 0
    graph: int = 0
    fused: int = 0
    below_floor: int = 0
    returned: int = 0
    ceiling: str = "C0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "vector": self.vector,
            "bm25": self.bm25,
            "graph": self.graph,
            "fused": self.fused,
            "below_floor": self.below_floor,
            "returned": self.returned,
            "ceiling": self.ceiling,
        }


class HybridRetriever:
    """Vector + BM25 + graph, filtered per branch, fused with RRF, then reranked."""

    def __init__(
        self,
        store: VectorStore,
        embeddings: Embeddings,
        *,
        graph: GraphRetriever | None = None,
        reranker: Reranker | None = None,
        hybrid: bool = True,
        rerank: bool = True,
        candidate_multiplier: int = 4,
        min_relevance: float = MIN_RELEVANCE,
    ) -> None:
        self._store = store
        self._embeddings = embeddings
        self._graph = graph
        self._reranker = reranker or LexicalReranker()
        self._hybrid = hybrid
        self._rerank = rerank
        self._multiplier = max(1, candidate_multiplier)
        self._min_relevance = min_relevance
        self.last_report = RetrievalReport()

    async def retrieve(self, request: RetrievalRequest) -> list[RetrievalResult]:
        """Everything the requester may see for this query, best first."""
        access = build_filter(request.identity, policy=request.policy)
        if not request.query.strip():
            return []

        # Retrieve wide, then rerank down: the reranker can only reorder what it is given.
        candidates = request.top_k * self._multiplier
        rankings: list[list[RetrievalResult]] = []

        vector_hits = await self._vector_branch(request.query, access, candidates)
        rankings.append(vector_hits)

        bm25_hits: list[RetrievalResult] = []
        graph_hits: list[RetrievalResult] = []
        if self._hybrid:
            bm25_hits = await self._bm25_branch(request.query, access, candidates)
            rankings.append(bm25_hits)
            if self._graph is not None:
                graph_hits = await self._graph.search(request.query, access, limit=candidates)
                rankings.append(graph_hits)

        fused = reciprocal_rank_fusion([r for r in rankings if r])
        if self._rerank and fused:
            reranked = await self._reranker.rerank(request.query, fused, limit=request.top_k)
            # The reranker's score is normalised query coverage, so the floor is
            # meaningful. RRF scores are not comparable across corpora, which is why the
            # floor only applies here and reranking is on by default.
            final = [r for r in reranked if r.score >= self._min_relevance]
        else:
            final = list(fused[: request.top_k])

        # Belt and braces. Every branch already filtered; this catches a future branch
        # that forgets, and costs one predicate per returned chunk.
        final = [r for r in final if access.permits(r.chunk)]

        self.last_report = RetrievalReport(
            vector=len(vector_hits),
            bm25=len(bm25_hits),
            graph=len(graph_hits),
            fused=len(fused),
            below_floor=max(0, min(len(fused), request.top_k) - len(final)),
            returned=len(final),
            ceiling=str(access.ceiling),
        )
        # Merge rather than splat both: `ceiling` appears in each, and duplicate
        # keywords are a TypeError, not a silent overwrite.
        get_metrics().retrieval_results.labels(tenant=access.tenant_id).observe(
            self.last_report.returned
        )
        log.info("knowledge.retrieved", **{**access.describe(), **self.last_report.to_dict()})
        return final

    async def _vector_branch(
        self, query: str, access: AccessFilter, limit: int
    ) -> list[RetrievalResult]:
        try:
            vector = await self._embeddings.embed_query(query)
        except ModelGatewayError as exc:
            # Losing embeddings degrades to lexical retrieval; it must not fail the query.
            log.warning("knowledge.embedding_unavailable", detail=str(exc))
            return []
        return await self._store.search(vector, access, limit=limit)

    async def _bm25_branch(
        self, query: str, access: AccessFilter, limit: int
    ) -> list[RetrievalResult]:
        # scroll() applies the same filter, so the BM25 index is built over exactly the
        # chunks this requester may see -- never over the whole corpus.
        visible = await self._store.scroll(access, limit=2000)
        if not visible:
            return []
        return BM25Index(visible).search(query, limit=limit)

    def cumulative_classification(self, results: Sequence[RetrievalResult]) -> Classification:
        """The highest classification among what was retrieved.

        The caller folds this into the task's running maximum, which is what decides the
        model backend.
        """
        levels = [r.chunk.classification for r in results]
        return max(levels) if levels else Classification.C0
