"""Knowledge: chunking, classification, retrieval and — above all — access control.

F3's exit criteria. The second one is the whole reason this layer is shaped the way it
is: a user without permission must not learn that a document exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_forge.core.classification import Classification
from agent_forge.core.state import Identity
from agent_forge.knowledge import (
    AccessControl,
    CagPreloader,
    Chunk,
    Classifier,
    Document,
    IngestionPipeline,
    PolicyView,
    SourceRef,
    build_chunks,
    build_filter,
    chunk_text,
    to_qdrant_filter,
)
from agent_forge.knowledge.graphrag import InMemoryGraphStore, extract_entities
from agent_forge.knowledge.rag import (
    BM25Index,
    HashingEmbeddings,
    HybridRetriever,
    InMemoryVectorStore,
    LexicalReranker,
    RetrievalRequest,
    reciprocal_rank_fusion,
)
from agent_forge.knowledge.sources import FolderSourceReader, SyncCursor, parse_text

TENANT = "acme-mx"

# Two people in the same area at the same clearance, with different reach.
LEADER = Identity(
    tenant_id=TENANT,
    user_id="lider-1",
    groups=("finanzas", "finanzas-lideres"),
    classification_ceiling=Classification.C3,
    authenticated=True,
)
ANALYST = Identity(
    tenant_id=TENANT,
    user_id="analista-1",
    groups=("finanzas",),
    classification_ceiling=Classification.C3,
    authenticated=True,
)
OUTSIDER = Identity(
    tenant_id=TENANT,
    user_id="ventas-1",
    groups=("ventas",),
    classification_ceiling=Classification.C3,
    authenticated=True,
)
ANONYMOUS = Identity(tenant_id=TENANT, user_id=None, groups=(), authenticated=False)
# Same group as the analyst, one clearance level higher. Used to show that the ceiling
# and the ACL are independent conditions.
CLEARED = ANALYST.model_copy(update={"classification_ceiling": Classification.C4})


def make_chunk(
    text: str,
    *,
    source: str = "folder:///data/politica.md",
    chunk_id: str = "p0",
    groups: tuple[str, ...] = ("finanzas",),
    classification: Classification = Classification.C2,
    tenant: str = TENANT,
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        source_id=source,
        tenant_id=tenant,
        text=text,
        title="Politica",
        acl=AccessControl(groups=frozenset(groups)),
        classification=classification,
    )


# ------------------------------------------------------------------- chunking


def test_short_text_is_one_chunk() -> None:
    assert chunk_text("Una politica breve.") == ["Una politica breve."]


def test_long_text_splits_on_paragraphs_and_overlaps() -> None:
    paragraphs = "\n\n".join(f"Parrafo {i}. " + "x" * 400 for i in range(6))

    chunks = chunk_text(paragraphs, max_chars=600, overlap=80)

    assert len(chunks) > 1
    assert all(len(c) <= 700 for c in chunks), "overlap must not blow past the budget"
    assert all(c.strip() for c in chunks)


def test_a_giant_paragraph_never_splits_mid_word() -> None:
    text = " ".join(f"palabra{i}" for i in range(500))

    chunks = chunk_text(text, max_chars=200, overlap=0)

    assert len(chunks) > 1
    for chunk in chunks:
        assert not chunk.startswith("bra"), "split landed inside a word"


def test_chunks_inherit_the_documents_acl_and_classification() -> None:
    document = Document(
        source=SourceRef("folder", "/data/x.md"),
        tenant_id=TENANT,
        title="x",
        text="parrafo uno\n\nparrafo dos",
        acl=AccessControl(groups=frozenset({"finanzas-lideres"})),
        classification=Classification.C3,
    )

    chunks = build_chunks(document, max_chars=20)

    assert len(chunks) >= 2
    assert all(c.classification == Classification.C3 for c in chunks)
    assert all(c.acl.groups == frozenset({"finanzas-lideres"}) for c in chunks)


def test_chunk_ids_are_stable_so_reingestion_replaces() -> None:
    first = make_chunk("texto")
    second = make_chunk("texto distinto")

    assert first.point_id == second.point_id


# --------------------------------------------------------------- classification


async def test_a_marker_in_the_header_wins() -> None:
    classifier = Classifier(default=Classification.C1, use_model=False)
    document = Document(
        source=SourceRef("folder", "/x"),
        tenant_id=TENANT,
        title="x",
        text="CONFIDENCIAL\n\nContenido del documento.",
    )

    result = await classifier.classify(document)

    assert result.classification == Classification.C2
    assert result.method == "marker"


async def test_the_highest_marker_wins_when_several_appear() -> None:
    classifier = Classifier(default=Classification.C1, use_model=False)
    document = Document(
        source=SourceRef("folder", "/x"),
        tenant_id=TENANT,
        title="x",
        text="Documento publico con anexo SECRETO adjunto.",
    )

    assert (await classifier.classify(document)).classification == Classification.C4


async def test_a_source_sensitivity_label_is_honoured() -> None:
    classifier = Classifier(default=Classification.C1, use_model=False)
    document = Document(
        source=SourceRef("sharepoint", "/x"),
        tenant_id=TENANT,
        title="x",
        text="sin marcador",
        metadata={"sensitivity_label": "Restricted"},
    )

    result = await classifier.classify(document)

    assert result.classification == Classification.C3
    assert result.method == "source_label"


async def test_a_manual_override_beats_everything() -> None:
    classifier = Classifier(
        default=Classification.C1,
        use_model=False,
        overrides={"folder:///x": Classification.C4},
    )
    document = Document(
        source=SourceRef("folder", "/x"), tenant_id=TENANT, title="x", text="PUBLICO"
    )

    result = await classifier.classify(document)

    assert result.classification == Classification.C4
    assert result.method == "override"


async def test_unclassifiable_content_falls_back_to_the_profile_default() -> None:
    classifier = Classifier(default=Classification.C2, use_model=False)
    document = Document(
        source=SourceRef("folder", "/x"), tenant_id=TENANT, title="x", text="texto neutro"
    )

    result = await classifier.classify(document)

    assert result.classification == Classification.C2
    assert result.method == "default"


def test_a_c0_ingestion_default_is_rejected_at_construction() -> None:
    """Defaulting to public is the one direction of error that leaks."""
    with pytest.raises(ValueError, match="must not be C0"):
        Classifier(default=Classification.C0)


# ------------------------------------------------------------- access control


def test_the_filter_admits_what_the_requester_may_see() -> None:
    access = build_filter(ANALYST)
    allowed = make_chunk("visible", groups=("finanzas",), classification=Classification.C2)

    assert access.permits(allowed)


@pytest.mark.parametrize(
    ("label", "chunk_kwargs"),
    [
        ("another tenant", {"tenant": "otra-empresa"}),
        ("above the ceiling", {"classification": Classification.C4}),
        ("a group they lack", {"groups": ("finanzas-lideres",)}),
    ],
)
def test_the_filter_rejects(label: str, chunk_kwargs: dict[str, Any]) -> None:
    access = build_filter(ANALYST)

    assert not access.permits(make_chunk("secreto", **chunk_kwargs)), label


def test_an_anonymous_requester_matches_nothing() -> None:
    access = build_filter(ANONYMOUS)

    assert access.ceiling == Classification.C0
    assert not access.permits(make_chunk("cualquier cosa", classification=Classification.C0))


def test_policy_may_tighten_the_ceiling_but_not_raise_it() -> None:
    tightened = build_filter(LEADER, policy=PolicyView(ceiling_override=Classification.C1))
    attempted = build_filter(ANALYST, policy=PolicyView(ceiling_override=Classification.C4))

    assert tightened.ceiling == Classification.C1
    assert attempted.ceiling == Classification.C3, "policy must not exceed the identity"


def test_policy_can_deny_a_specific_source() -> None:
    access = build_filter(
        ANALYST, policy=PolicyView(denied_sources=frozenset({"folder:///data/politica.md"}))
    )

    assert not access.permits(make_chunk("texto"))


def test_deny_all_produces_a_filter_that_matches_nothing() -> None:
    access = build_filter(LEADER, policy=PolicyView(deny_all=True, reasons=("pdp down",)))

    assert access.allow_nothing
    assert not access.permits(make_chunk("texto", classification=Classification.C0))


def test_the_qdrant_filter_encodes_the_same_conditions() -> None:
    payload = to_qdrant_filter(build_filter(ANALYST))

    must = {c["key"] for c in payload["must"]}
    assert must == {"tenant_id", "classification"}
    # The ACL conditions live inside min_should with a count, not in a plain `should`.
    # A plain `should` is optional, which would turn the ACL into a ranking hint.
    assert payload["min_should"]["min_count"] == 1
    assert any(c["key"] == "acl_groups" for c in payload["min_should"]["conditions"])
    assert "should" not in payload


def test_the_qdrant_filter_for_deny_all_cannot_match() -> None:
    payload = to_qdrant_filter(build_filter(LEADER, policy=PolicyView(deny_all=True)))

    assert payload["must"][0]["match"]["value"] == "\x00never"


# ------------------------------------------------------------------- retrieval


async def _index(store: InMemoryVectorStore, chunks: list[Chunk]) -> HashingEmbeddings:
    embeddings = HashingEmbeddings(dimension=256)
    vectors = await embeddings.embed([c.text for c in chunks])
    for chunk, vector in zip(chunks, vectors, strict=True):
        chunk.vector = tuple(vector)
    await store.ensure_collection(embeddings.dimension)
    await store.upsert(chunks)
    return embeddings


@pytest.fixture
async def retriever() -> Any:
    store = InMemoryVectorStore()
    chunks = [
        make_chunk(
            "El limite de viaticos nacionales es de 1500 MXN por dia.",
            source="folder:///data/viaticos.md",
            chunk_id="p0",
            groups=("finanzas",),
            classification=Classification.C2,
        ),
        make_chunk(
            "La nomina del equipo directivo se revisa cada trimestre.",
            source="folder:///data/nomina.md",
            chunk_id="p0",
            groups=("finanzas-lideres",),
            classification=Classification.C2,
        ),
        make_chunk(
            "El presupuesto secreto de adquisiciones asciende a 40 millones.",
            source="folder:///data/adquisiciones.md",
            chunk_id="p0",
            groups=("finanzas",),
            classification=Classification.C4,
        ),
    ]
    embeddings = await _index(store, chunks)
    return HybridRetriever(store, embeddings, reranker=LexicalReranker())


async def test_a_question_about_an_ingested_document_answers_with_its_citation(
    retriever: Any,
) -> None:
    """F3 exit criterion, first half."""
    results = await retriever.retrieve(
        RetrievalRequest(query="cual es el limite de viaticos", identity=ANALYST, top_k=3)
    )

    assert results
    assert results[0].citation.reference == "folder:///data/viaticos.md#p0"
    assert "1500 MXN" in results[0].text


async def test_a_requester_without_the_group_learns_nothing_about_the_document(
    retriever: Any,
) -> None:
    """F3 exit criterion, second half, and the reason for the whole design.

    The analyst asks the leaders' question directly. The answer must be indistinguishable
    from an empty corpus: not a redacted hit, not a shorter list -- nothing.
    """
    leader_view = await retriever.retrieve(
        RetrievalRequest(query="nomina del equipo directivo", identity=LEADER, top_k=5)
    )
    analyst_view = await retriever.retrieve(
        RetrievalRequest(query="nomina del equipo directivo", identity=ANALYST, top_k=5)
    )

    assert any(r.chunk.source_id.endswith("nomina.md") for r in leader_view)
    assert not any(r.chunk.source_id.endswith("nomina.md") for r in analyst_view)
    assert all("nomina" not in r.text.lower() for r in analyst_view)


async def test_an_outsider_to_the_area_gets_an_empty_corpus(retriever: Any) -> None:
    results = await retriever.retrieve(
        RetrievalRequest(query="limite de viaticos", identity=OUTSIDER, top_k=5)
    )

    assert results == []


async def test_the_ceiling_hides_content_above_it(retriever: Any) -> None:
    """Same ACL group, different clearance: the ceiling alone decides."""
    with_clearance = await retriever.retrieve(
        RetrievalRequest(query="presupuesto secreto adquisiciones", identity=CLEARED, top_k=5)
    )
    without = await retriever.retrieve(
        RetrievalRequest(query="presupuesto secreto adquisiciones", identity=ANALYST, top_k=5)
    )

    assert any(r.chunk.classification == Classification.C4 for r in with_clearance)
    assert all(r.chunk.classification <= Classification.C3 for r in without)
    assert not any(r.chunk.source_id.endswith("adquisiciones.md") for r in without)


async def test_retrieval_reports_the_highest_classification_it_returned(
    retriever: Any,
) -> None:
    """This is what the graph folds into the task maximum, and it decides the backend."""
    results = await retriever.retrieve(
        RetrievalRequest(query="presupuesto secreto adquisiciones", identity=CLEARED, top_k=5)
    )

    assert retriever.cumulative_classification(results) == Classification.C4


async def test_an_empty_query_retrieves_nothing(retriever: Any) -> None:
    assert await retriever.retrieve(RetrievalRequest(query="   ", identity=ANALYST)) == []


# ----------------------------------------------------------------- components


def test_bm25_ranks_the_matching_chunk_first() -> None:
    chunks = [
        make_chunk("politica de viaticos y gastos de viaje", chunk_id="a"),
        make_chunk("procedimiento de alta de proveedores", chunk_id="b"),
    ]

    ranked = BM25Index(chunks).search("viaticos")

    assert ranked
    assert ranked[0].chunk.chunk_id == "a"


def test_bm25_ignores_stopwords_and_accents() -> None:
    chunks = [make_chunk("el limite de viáticos", chunk_id="a")]

    assert BM25Index(chunks).search("¿cuál es el límite de viaticos?")


def test_rrf_rewards_agreement_between_rankers() -> None:
    a = make_chunk("uno", chunk_id="a")
    b = make_chunk("dos", chunk_id="b")
    from agent_forge.knowledge.documents import RetrievalResult

    c = make_chunk("tres", chunk_id="c")

    # `a` is top in one ranking and second in the other; `c` is only ever last. RRF has
    # to prefer the one both rankers liked, whatever the raw scores said.
    ranking_one = [RetrievalResult(a, 0.9), RetrievalResult(b, 0.5), RetrievalResult(c, 0.1)]
    ranking_two = [RetrievalResult(b, 0.4), RetrievalResult(a, 0.3), RetrievalResult(c, 0.2)]

    fused = reciprocal_rank_fusion([ranking_one, ranking_two])

    assert [r.chunk.chunk_id for r in fused][:2] == ["a", "b"]
    assert fused[-1].chunk.chunk_id == "c"
    assert fused[0].score > fused[-1].score


async def test_the_reranker_prefers_the_chunk_covering_more_of_the_query() -> None:
    from agent_forge.knowledge.documents import RetrievalResult

    partial = make_chunk("politica de viaticos", chunk_id="a")
    complete = make_chunk("politica de viaticos nacionales limite diario", chunk_id="b")

    ranked = await LexicalReranker().rerank(
        "limite diario de viaticos nacionales",
        [RetrievalResult(partial, 0.9), RetrievalResult(complete, 0.1)],
        limit=2,
    )

    assert ranked[0].chunk.chunk_id == "b"


async def test_hashing_embeddings_are_deterministic_and_normalised() -> None:
    embeddings = HashingEmbeddings(dimension=64)

    first = await embeddings.embed_query("limite de viaticos")
    second = await embeddings.embed_query("limite de viaticos")

    assert first == second
    assert abs(sum(v * v for v in first) - 1.0) < 1e-9


async def test_similar_text_embeds_closer_than_unrelated_text() -> None:
    from agent_forge.knowledge.rag import cosine

    embeddings = HashingEmbeddings(dimension=512)
    base, near, far = await embeddings.embed(
        ["limite de viaticos nacionales", "limite de viaticos", "receta de paella"]
    )

    assert cosine(base, near) > cosine(base, far)


# --------------------------------------------------------------------- graph


def test_entity_extraction_finds_systems_and_acronyms() -> None:
    entities, relations = extract_entities("Falla el ERP de SAP tras el cambio en la VPN")

    names = {e.name.lower() for e in entities}
    assert "erp" in names
    assert "vpn" in names
    assert relations


async def test_the_graph_respects_the_same_access_filter() -> None:
    """A traversal must not become a second, unguarded door into the corpus."""
    graph = InMemoryGraphStore()
    restricted = make_chunk(
        "El ERP de nomina expone los sueldos del equipo directivo.",
        source="folder:///data/nomina.md",
        groups=("finanzas-lideres",),
    )
    entities, relations = extract_entities(restricted.text)
    await graph.upsert(TENANT, restricted, entities, relations)

    leader = await graph.search("problema con el ERP", build_filter(LEADER))
    analyst = await graph.search("problema con el ERP", build_filter(ANALYST))

    assert leader
    assert analyst == []


# ----------------------------------------------------------------------- CAG


async def test_cag_preloads_only_what_matches_and_is_permitted() -> None:
    store = InMemoryVectorStore()
    stable = make_chunk(
        "Politica de viaticos vigente.",
        source="folder:///data/politicas/viaticos.md",
        groups=("finanzas",),
    )
    stable.metadata["path"] = "politicas/viaticos.md"
    restricted = make_chunk(
        "Politica de nomina.",
        source="folder:///data/politicas/nomina.md",
        chunk_id="p1",
        groups=("finanzas-lideres",),
    )
    restricted.metadata["path"] = "politicas/nomina.md"
    volatile = make_chunk("Nota del dia.", source="folder:///data/notas/hoy.md", chunk_id="p2")
    volatile.metadata["path"] = "notas/hoy.md"
    await _index(store, [stable, restricted, volatile])

    preloader = CagPreloader(store=store, patterns=("politicas/*",), enabled=True)
    corpus = await preloader.load(build_filter(ANALYST))

    assert corpus.citations() == ["folder:///data/politicas/viaticos.md#p0"]
    assert "nomina" not in corpus.as_context().lower()
    assert "nunca instrucciones" in corpus.as_context()


async def test_cag_respects_its_budget_and_says_so() -> None:
    store = InMemoryVectorStore()
    chunks = [
        make_chunk("x" * 500, source=f"folder:///data/p/{i}.md", chunk_id=f"p{i}")
        for i in range(10)
    ]
    for index, chunk in enumerate(chunks):
        chunk.metadata["path"] = f"p/{index}.md"
    await _index(store, chunks)

    corpus = await CagPreloader(
        store=store, patterns=("p/*",), enabled=True, budget_chars=1200
    ).load(build_filter(ANALYST))

    assert corpus.truncated
    assert corpus.characters <= 1200


async def test_cag_disabled_returns_nothing() -> None:
    corpus = await CagPreloader(store=InMemoryVectorStore(), enabled=False).load(
        build_filter(ANALYST)
    )

    assert corpus.empty


# ----------------------------------------------------------------- ingestion


async def test_a_folder_is_ingested_and_becomes_retrievable(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "viaticos.md").write_text(
        "# Politica de viaticos\n\nEl limite nacional es de 1500 MXN por dia.",
        encoding="utf-8",
    )
    store = InMemoryVectorStore()
    pipeline = IngestionPipeline(
        vector_store=store,
        embeddings=HashingEmbeddings(dimension=256),
        classifier=Classifier(default=Classification.C2, use_model=False),
    )
    reader = FolderSourceReader(
        corpus,
        tenant_id=TENANT,
        acl_groups=["finanzas"],
        default_classification=Classification.C2,
    )

    report = await pipeline.sync(reader, SyncCursor())

    assert report.documents == 1
    assert report.chunks >= 1
    assert report.failed == 0
    assert await store.count(TENANT) >= 1


async def test_an_unchanged_document_is_not_reingested(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("contenido estable", encoding="utf-8")
    pipeline = IngestionPipeline(
        vector_store=InMemoryVectorStore(),
        embeddings=HashingEmbeddings(dimension=64),
        classifier=Classifier(default=Classification.C2, use_model=False),
    )
    reader = FolderSourceReader(corpus, tenant_id=TENANT, acl_groups=["finanzas"])
    cursor = SyncCursor()

    first = await pipeline.sync(reader, cursor)
    second = await pipeline.sync(reader, cursor)

    assert first.documents == 1
    assert second.documents == 0, "an unchanged corpus must not be re-embedded"


async def test_reingesting_a_shrunken_document_removes_the_old_chunks(
    tmp_path: Path,
) -> None:
    """A section deleted from a document must stop being retrievable."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    target = corpus / "a.md"
    target.write_text("\n\n".join(f"seccion {i} " + "y" * 400 for i in range(6)), "utf-8")
    store = InMemoryVectorStore()
    pipeline = IngestionPipeline(
        vector_store=store,
        embeddings=HashingEmbeddings(dimension=64),
        classifier=Classifier(default=Classification.C2, use_model=False),
        chunk_chars=500,
    )
    reader = FolderSourceReader(corpus, tenant_id=TENANT, acl_groups=["finanzas"])
    cursor = SyncCursor()
    await pipeline.sync(reader, cursor)
    before = await store.count(TENANT)

    target.write_text("solo queda esto", encoding="utf-8")
    await pipeline.sync(reader, cursor)

    assert await store.count(TENANT) < before
    assert await store.count(TENANT) == 1


async def test_an_unparseable_format_is_skipped_not_ingested_blank(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "imagen.png").write_bytes(b"\x89PNG not text")
    (corpus / "bueno.md").write_text("contenido valido", encoding="utf-8")
    store = InMemoryVectorStore()
    pipeline = IngestionPipeline(
        vector_store=store,
        embeddings=HashingEmbeddings(dimension=64),
        classifier=Classifier(default=Classification.C2, use_model=False),
    )

    report = await pipeline.sync(
        FolderSourceReader(corpus, tenant_id=TENANT, acl_groups=["finanzas"]), SyncCursor()
    )

    assert report.documents == 1
    assert report.failed == 0


def test_unknown_formats_return_none_rather_than_empty_text() -> None:
    assert parse_text(Path("x.png"), b"\x89PNG") is None
    assert parse_text(Path("x.md"), b"# hola") == "# hola"


async def test_forget_tenant_clears_the_corpus_from_both_stores() -> None:
    store = InMemoryVectorStore()
    graph = InMemoryGraphStore()
    pipeline = IngestionPipeline(
        vector_store=store,
        embeddings=HashingEmbeddings(dimension=64),
        classifier=Classifier(default=Classification.C2, use_model=False),
        graph_store=graph,
    )
    await pipeline.ingest_document(
        Document(
            source=SourceRef("folder", "/data/x.md"),
            tenant_id=TENANT,
            title="x",
            text="El ERP de SAP gestiona los viaticos.",
            acl=AccessControl(groups=frozenset({"finanzas"})),
        )
    )
    assert await store.count(TENANT) >= 1

    await pipeline.forget_tenant(TENANT)

    assert await store.count(TENANT) == 0
    assert await graph.search("ERP", build_filter(ANALYST)) == []
