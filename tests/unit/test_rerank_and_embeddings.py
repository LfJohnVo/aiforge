"""Which model serves embeddings and reranking, and what happens when none does.

Two failure shapes are covered here, and the second is the one that actually bit:

*Loud* failure -- an external reranker asked to score classified chunks. It must raise,
because a cross-encoder reads the query and every candidate chunk in full, so it is the
same leak as an external chat model with none of the visibility.

*Quiet* failure -- no gateway at all. Retrieval still returns results, ranked by lexical
hashing, so the cell looks like a working RAG right up until someone phrases a question
differently from the document. In production that is a boot failure now, not a log line.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from agent_forge.core.classification import Classification
from agent_forge.core.errors import (
    CapabilityUnavailableError,
    ModelGatewayError,
    SovereigntyError,
)
from agent_forge.gateway.litellm_client import LiteLLMTransport
from agent_forge.knowledge.documents import Chunk, RetrievalResult
from agent_forge.knowledge.rag import GatewayReranker, LexicalReranker, build_embeddings
from tests.support import (
    EXTERNAL_RERANK,
    LOCAL_FAST,
    FakeTransport,
    make_gateway,
)

BASE = "http://litellm:4000"
RERANK = f"{BASE}/v1/rerank"

pytestmark = pytest.mark.anyio


def chunk(text: str, classification: Classification = Classification.C1) -> RetrievalResult:
    return RetrievalResult(
        chunk=Chunk(
            chunk_id=text[:8],
            source_id="doc-1",
            tenant_id="acme-mx",
            text=text,
            classification=classification,
        ),
        score=0.5,
        retriever="fused",
    )


# ----------------------------------------------------------------------- the wire


@respx.mock
async def test_rerank_scores_come_back_in_the_order_the_documents_went_out() -> None:
    """The backend answers sorted by relevance; the caller indexes by position."""
    respx.post(RERANK).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.4},
                    {"index": 1, "relevance_score": 0.1},
                ]
            },
        )
    )

    scores = await LiteLLMTransport(BASE, "sk-test").rerank(
        "pregunta", ["a", "b", "c"], "local/rerank"
    )

    assert scores == [0.4, 0.1, 0.9]


@respx.mock
async def test_a_document_the_backend_dropped_scores_zero_rather_than_shifting_the_list() -> None:
    """`top_n` truncation must not silently promote the wrong chunk into a citation."""
    respx.post(RERANK).mock(
        return_value=httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.8}]})
    )

    scores = await LiteLLMTransport(BASE, "sk-test").rerank(
        "pregunta", ["a", "b", "c"], "local/rerank"
    )

    assert scores == [0.0, 0.8, 0.0]


@respx.mock
async def test_an_unreachable_rerank_backend_raises_rather_than_scoring_zero() -> None:
    respx.post(RERANK).mock(side_effect=httpx.ConnectError("nope"))

    with pytest.raises(ModelGatewayError):
        await LiteLLMTransport(BASE, "sk-test").rerank("q", ["a"], "local/rerank")


# ------------------------------------------------------------------- sovereignty


async def test_classified_chunks_are_never_reranked_by_an_external_model() -> None:
    """Invariant 1, on the path that is easiest to forget."""
    gateway = make_gateway(backends=[LOCAL_FAST, EXTERNAL_RERANK])

    with pytest.raises(SovereigntyError):
        await gateway.rerank(
            "pregunta",
            ["contenido reservado"],
            classification=Classification.C3,
            preferred=("cohere/rerank",),
        )


async def test_the_reranker_routes_on_the_highest_classification_it_was_handed() -> None:
    """One C3 chunk among C0s is still C3: the reranker sees them all together."""
    gateway = make_gateway(backends=[LOCAL_FAST, EXTERNAL_RERANK])
    reranker = GatewayReranker(gateway, alias="cohere/rerank")

    results = [chunk("publico", Classification.C0), chunk("reservado", Classification.C3)]
    ranked = await reranker.rerank("pregunta", results, limit=2)

    # Refused, so it degraded rather than sending C3 to Cohere -- and still answered.
    assert len(ranked) == 2
    assert all(r.retriever == "reranked" for r in ranked)


# --------------------------------------------------------------------- behaviour


async def test_the_gateway_reranker_reorders_by_the_scores_it_receives() -> None:
    class Scoring(FakeTransport):
        async def rerank(self, query: str, documents: Any, model: str) -> list[float]:
            return [0.1, 0.99, 0.5][: len(documents)]

    reranker = GatewayReranker(make_gateway(Scoring()))
    ranked = await reranker.rerank("pregunta", [chunk("uno"), chunk("dos"), chunk("tres")], limit=3)

    assert [r.chunk.text for r in ranked] == ["dos", "tres", "uno"]


async def test_a_reranker_that_is_down_costs_quality_not_the_answer() -> None:
    """A dead reranker must not turn a good retrieval into a 500."""

    class Broken(FakeTransport):
        async def rerank(self, query: str, documents: Any, model: str) -> list[float]:
            raise ModelGatewayError("rerank backend unreachable")

    reranker = GatewayReranker(make_gateway(Broken()))
    results = [chunk("politica de viaticos"), chunk("factura vencida")]
    ranked = await reranker.rerank("factura vencida", results, limit=2)

    assert [r.chunk.text for r in ranked] == ["factura vencida", "politica de viaticos"]


async def test_the_lexical_reranker_remains_the_default_when_no_model_is_configured() -> None:
    """`RERANK_MODEL` empty is a decision, not an omission: no second model needed."""
    ranked = await LexicalReranker().rerank(
        "factura vencida",
        [chunk("politica de viaticos"), chunk("la factura vencida del cliente")],
        limit=1,
    )

    assert "factura" in ranked[0].chunk.text


async def test_a_chunk_the_tokenizer_cannot_split_sinks_but_is_never_dropped() -> None:
    """A reranker reorders candidates; one that deletes them loses the only source.

    The guard that used to skip these protected nothing -- the coverage divisor is the
    query length, not the chunk's -- and it meant a shortlist could come back empty.
    """
    results = [chunk("de la el"), chunk("la factura vencida")]
    ranked = await LexicalReranker().rerank("factura vencida", results, limit=5)

    assert len(ranked) == 2
    assert ranked[0].chunk.text == "la factura vencida"


# --------------------------------------------------------------------- embeddings


def test_production_refuses_to_index_with_lexical_hashing() -> None:
    """The quiet failure. Retrieval would work, and be wrong in a way nobody notices."""
    with pytest.raises(CapabilityUnavailableError):
        build_embeddings(None, strict=True)


def test_development_still_falls_back_so_the_quickstart_runs_without_a_gateway() -> None:
    embedder = build_embeddings(None, strict=False)

    assert embedder.dimension == 1024


def test_the_embedding_alias_is_configurable_because_it_is_a_deployment_choice() -> None:
    """One vLLM serves one model, so a single-GPU host serves embeddings elsewhere."""
    embedder = build_embeddings(make_gateway(), alias="local/embeddings-dev")

    assert embedder._alias == "local/embeddings-dev"
