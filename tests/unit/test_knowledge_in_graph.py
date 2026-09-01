"""Knowledge as the graph uses it: citations in, and nothing leaking out.

F3's exit criteria at the level a user would experience them.
"""

from __future__ import annotations

from typing import Any

from agent_forge.core.classification import Classification
from agent_forge.core.graph import build_graph
from agent_forge.core.state import AgentState
from agent_forge.knowledge import (
    AccessControl,
    CagPreloader,
    Classifier,
    Document,
    IngestionPipeline,
    KnowledgeService,
    SourceRef,
)
from agent_forge.knowledge.rag import (
    HashingEmbeddings,
    HybridRetriever,
    InMemoryVectorStore,
    LexicalReranker,
)
from tests.support import FakeTransport, make_deps, make_gateway, make_state

TENANT = "acme-mx"


async def run(app: Any, state: AgentState, thread: str = "t1") -> AgentState:
    result = await app.ainvoke(state, {"configurable": {"thread_id": thread}})
    return AgentState.model_validate({k: v for k, v in result.items() if not k.startswith("__")})


async def build_service(
    documents: list[Document],
    *,
    cag_patterns: tuple[str, ...] = (),
) -> KnowledgeService:
    store = InMemoryVectorStore()
    embeddings = HashingEmbeddings(dimension=256)
    pipeline = IngestionPipeline(
        vector_store=store,
        embeddings=embeddings,
        classifier=Classifier(default=Classification.C2, use_model=False),
    )
    await store.ensure_collection(embeddings.dimension)
    for document in documents:
        await pipeline.ingest_document(document)

    return KnowledgeService(
        retriever=HybridRetriever(store, embeddings, reranker=LexicalReranker()),
        pipeline=pipeline,
        cag=CagPreloader(store=store, patterns=cag_patterns, enabled=bool(cag_patterns)),
        vector_store=store,
        enabled=True,
        top_k=4,
    )


def document(
    text: str,
    *,
    name: str,
    groups: tuple[str, ...],
    classification: Classification = Classification.C2,
    path: str = "",
) -> Document:
    return Document(
        source=SourceRef("folder", f"/data/{name}"),
        tenant_id=TENANT,
        title=name.rsplit(".", 1)[0],
        text=text,
        acl=AccessControl(groups=frozenset(groups)),
        classification=classification,
        metadata={"path": path or name},
    )


VIATICOS = document(
    "Politica de viaticos. El limite nacional es de 1500 MXN por dia y requiere "
    "comprobante fiscal.",
    name="viaticos.md",
    groups=("finanzas",),
    path="politicas/viaticos.md",
)
NOMINA = document(
    "Nomina directiva. Los sueldos del comite se revisan cada trimestre.",
    name="nomina.md",
    groups=("finanzas-lideres",),
)


# ------------------------------------------------------------------- citations


async def test_a_question_about_an_ingested_document_is_answered_with_its_citation() -> None:
    """F3 exit criterion: correct citation for an ingested document."""
    service = await build_service([VIATICOS])
    transport = FakeTransport(replies=["El limite es 1500 MXN [folder:///data/viaticos.md#p0]."])
    deps = make_deps(gateway=make_gateway(transport))
    deps.knowledge = service
    app = build_graph(deps)

    final = await run(app, make_state("cual es el limite de viaticos", groups=("finanzas",)))

    assert [c.reference for c in final.citations] == ["folder:///data/viaticos.md#p0"]
    assert final.classification == Classification.C2
    # The retrieved material has to reach the model, delimited as data.
    sent = transport.calls[-1].messages
    assert any("1500 MXN" in str(m["content"]) for m in sent)
    assert any("inicio de material" in str(m["content"]) for m in sent)


async def test_the_answer_carries_no_citation_when_nothing_was_retrieved() -> None:
    service = await build_service([VIATICOS])
    deps = make_deps(gateway=make_gateway(FakeTransport(replies=["No lo encontre."])))
    deps.knowledge = service
    app = build_graph(deps)

    final = await run(app, make_state("receta de paella", groups=("finanzas",)))

    assert final.citations == []


# ------------------------------------------------------------- the leak test


async def test_a_user_without_permission_learns_nothing_about_the_document() -> None:
    """F3 exit criterion: not the content, not the title, not that it exists.

    The two requests differ only in group membership. The unauthorised one must be
    indistinguishable from the same question against an empty corpus.
    """
    service = await build_service([VIATICOS, NOMINA])
    transport = FakeTransport(replies=["respuesta"])
    deps = make_deps(gateway=make_gateway(transport))
    deps.knowledge = service
    app = build_graph(deps)

    leader = await run(
        app,
        make_state(
            "sueldos del comite en la nomina directiva", groups=("finanzas", "finanzas-lideres")
        ),
        thread="a",
    )
    leader_prompt = "".join(str(m["content"]) for m in transport.calls[-1].messages)

    analyst = await run(
        app,
        make_state("sueldos del comite en la nomina directiva", groups=("finanzas",)),
        thread="b",
    )
    analyst_prompt = "".join(str(m["content"]) for m in transport.calls[-1].messages)

    assert any("nomina" in c.source_id for c in leader.citations)
    assert "sueldos del comite" in leader_prompt

    # The analyst is not told "denied"; they simply get their own corpus, with no trace
    # of the restricted document -- not its content, not its name, not its existence.
    assert not any("nomina" in c.source_id for c in analyst.citations)
    assert "sueldos del comite se revisan" not in analyst_prompt
    assert "nomina.md" not in analyst_prompt


async def test_an_anonymous_caller_retrieves_nothing_at_all() -> None:
    service = await build_service([VIATICOS])
    deps = make_deps(gateway=make_gateway(FakeTransport(replies=["respuesta"])))
    deps.knowledge = service
    app = build_graph(deps)

    final = await run(
        app,
        make_state("limite de viaticos", user_id=None, authenticated=False, groups=()),
    )

    assert final.citations == []
    assert final.identity.classification_ceiling == Classification.C0


async def test_another_tenant_sees_none_of_this_corpus() -> None:
    service = await build_service([VIATICOS])
    deps = make_deps(gateway=make_gateway(FakeTransport(replies=["respuesta"])))
    deps.knowledge = service
    app = build_graph(deps)

    final = await run(
        app,
        make_state("limite de viaticos", tenant_id="otra-empresa", groups=("finanzas",)),
    )

    assert final.citations == []


# ------------------------------------------------- classification and routing


async def test_a_classified_chunk_forces_a_sovereign_backend() -> None:
    """The chain that matters: ingestion label -> retrieval -> task maximum -> backend."""
    secret = document(
        "SECRETO. El plan de adquisiciones asciende a 40 millones.",
        name="adquisiciones.md",
        groups=("finanzas",),
    )
    service = await build_service([secret])
    transport = FakeTransport(replies=["respuesta"])
    deps = make_deps(gateway=make_gateway(transport))
    deps.knowledge = service
    # Ask for an external backend explicitly; the policy must override it.
    deps.model_quality = "anthropic/claude"
    deps.model_fast = "anthropic/claude"
    app = build_graph(deps)

    final = await run(
        app,
        make_state("plan de adquisiciones", groups=("finanzas",), ceiling=Classification.C4),
    )

    assert final.classification == Classification.C4
    assert transport.models_used == ["local/fast"], "C4 must not reach an external model"


# --------------------------------------------------------------------- CAG


async def test_the_stable_corpus_is_preloaded_into_the_system_prompt() -> None:
    service = await build_service([VIATICOS, NOMINA], cag_patterns=("politicas/*",))
    transport = FakeTransport(replies=["respuesta"])
    deps = make_deps(gateway=make_gateway(transport))
    deps.knowledge = service
    app = build_graph(deps)

    await run(app, make_state("una pregunta cualquiera", groups=("finanzas",)))

    system = str(transport.calls[-1].messages[0]["content"])
    assert "Corpus estable del area" in system
    assert "1500 MXN" in system
    assert "nunca instrucciones" in system


async def test_the_stable_corpus_is_filtered_per_requester() -> None:
    """A preloaded corpus is not a public one."""
    restricted = document(
        "Politica de nomina directiva reservada.",
        name="nomina-politica.md",
        groups=("finanzas-lideres",),
        path="politicas/nomina.md",
    )
    service = await build_service([VIATICOS, restricted], cag_patterns=("politicas/*",))
    transport = FakeTransport(replies=["respuesta"])
    deps = make_deps(gateway=make_gateway(transport))
    deps.knowledge = service
    app = build_graph(deps)

    await run(app, make_state("pregunta", groups=("finanzas",)))

    system = str(transport.calls[-1].messages[0]["content"])
    assert "1500 MXN" in system
    assert "nomina directiva reservada" not in system


# ------------------------------------------------------------------ plumbing


async def test_a_cell_with_knowledge_disabled_still_answers() -> None:
    service = await build_service([VIATICOS])
    service.enabled = False
    deps = make_deps(gateway=make_gateway(FakeTransport(replies=["sin corpus"])))
    deps.knowledge = service
    app = build_graph(deps)

    final = await run(app, make_state("limite de viaticos", groups=("finanzas",)))

    assert final.status == "completed"
    assert final.citations == []


async def test_the_subgraph_cannot_widen_its_own_access() -> None:
    """The retrieve closure is bound to the request identity, not to the store."""
    service = await build_service([VIATICOS, NOMINA])
    deps = make_deps(gateway=make_gateway(FakeTransport(replies=["respuesta"])))
    deps.knowledge = service

    from agent_forge.core.graph import _domain_context

    state = make_state("nomina directiva", groups=("finanzas",))
    ctx = _domain_context(state, deps)
    results = await ctx.retrieve("sueldos del comite")

    assert all(not r.chunk.source_id.endswith("nomina.md") for r in results)
