"""The knowledge adapters that talk to something: gateway, Graph, S3, Neo4j, Qdrant.

Their happy paths run in the integration suite against real services. What is tested here
is the part that integration tests are bad at: the failure and degradation behaviour, and
the wiring that decides which implementation a profile gets.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agent_forge.core.classification import Classification
from agent_forge.core.errors import ModelGatewayError
from agent_forge.knowledge import (
    AccessControl,
    Chunk,
    Classifier,
    Document,
    SourceRef,
    build_knowledge,
)
from agent_forge.knowledge.graphrag import (
    GraphStoreError,
    InMemoryGraphStore,
    Neo4jGraphStore,
    build_graph_store,
    extract_entities,
)
from agent_forge.knowledge.rag import (
    GatewayEmbeddings,
    HashingEmbeddings,
    InMemoryVectorStore,
    build_embeddings,
    build_vector_store,
    safe_embed,
)
from agent_forge.knowledge.rag.vector_store import QdrantVectorStore, VectorStoreError
from agent_forge.knowledge.sources import (
    S3SourceReader,
    SharePointSourceReader,
    SourceError,
    SyncCursor,
    build_reader,
)
from agent_forge.profile import load_profile
from tests.support import FakeTransport, make_gateway

TENANT = "acme-mx"
REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------- embeddings


class EmbeddingTransport(FakeTransport):
    """A transport whose embeddings are deterministic and inspectable."""

    def __init__(self) -> None:
        super().__init__()
        self.embedded: list[tuple[str, ...]] = []
        self.models: list[str] = []

    async def embed(self, texts: Any, model: str) -> list[list[float]]:
        self.embedded.append(tuple(texts))
        self.models.append(model)
        return [[float(len(t)), 1.0] for t in texts]


async def test_gateway_embeddings_route_through_a_sovereign_backend() -> None:
    """The vector looks like noise; the text that produced it did not."""
    transport = EmbeddingTransport()
    embeddings = GatewayEmbeddings(make_gateway(transport), dimension=2)

    vectors = await embeddings.embed(["uno", "dos"])

    assert vectors == [[3.0, 1.0], [3.0, 1.0]]
    assert transport.models == ["local/embeddings"]


async def test_gateway_embeddings_batch_large_inputs() -> None:
    transport = EmbeddingTransport()
    embeddings = GatewayEmbeddings(make_gateway(transport), dimension=2, batch_size=2)

    await embeddings.embed(["a", "b", "c", "d", "e"])

    assert [len(batch) for batch in transport.embedded] == [2, 2, 1]


async def test_embed_query_returns_a_single_vector() -> None:
    embeddings = GatewayEmbeddings(make_gateway(EmbeddingTransport()), dimension=2)

    assert await embeddings.embed_query("hola") == [4.0, 1.0]


async def test_a_gateway_outage_degrades_to_the_fallback_rather_than_failing() -> None:
    class BrokenEmbeddings:
        dimension = 8

        async def embed(self, texts: Any) -> list[list[float]]:
            raise ModelGatewayError("embedding backend down")

        async def embed_query(self, text: str) -> list[float]:
            raise ModelGatewayError("embedding backend down")

    vectors = await safe_embed(
        BrokenEmbeddings(), ["texto"], fallback=HashingEmbeddings(dimension=8)
    )

    assert len(vectors) == 1
    assert len(vectors[0]) == 8


async def test_without_a_fallback_the_outage_propagates() -> None:
    class BrokenEmbeddings:
        dimension = 8

        async def embed(self, texts: Any) -> list[list[float]]:
            raise ModelGatewayError("down")

        async def embed_query(self, text: str) -> list[float]:
            raise ModelGatewayError("down")

    with pytest.raises(ModelGatewayError):
        await safe_embed(BrokenEmbeddings(), ["texto"])


def test_build_embeddings_falls_back_to_hashing_without_a_gateway() -> None:
    assert isinstance(build_embeddings(None), HashingEmbeddings)


def test_build_embeddings_uses_the_gateway_when_there_is_one() -> None:
    assert isinstance(build_embeddings(make_gateway(FakeTransport())), GatewayEmbeddings)


# -------------------------------------------------------------- classifier


async def test_the_local_model_classifies_what_rules_cannot() -> None:
    gateway = make_gateway(FakeTransport(replies=["C3"]))
    classifier = Classifier(default=Classification.C1, gateway=gateway)
    document = Document(
        source=SourceRef("folder", "/x"), tenant_id=TENANT, title="x", text="texto neutro"
    )

    result = await classifier.classify(document)

    assert result.classification == Classification.C3
    assert result.method == "model"


async def test_the_model_can_never_classify_below_the_profile_default() -> None:
    """A model saying "public" about an unlabelled document is not enough to publish it."""
    gateway = make_gateway(FakeTransport(replies=["C0"]))
    classifier = Classifier(default=Classification.C2, gateway=gateway)
    document = Document(
        source=SourceRef("folder", "/x"), tenant_id=TENANT, title="x", text="texto neutro"
    )

    assert (await classifier.classify(document)).classification == Classification.C2


async def test_an_unparseable_model_answer_falls_through_to_the_default() -> None:
    gateway = make_gateway(FakeTransport(replies=["no tengo ni idea"]))
    classifier = Classifier(default=Classification.C2, gateway=gateway)
    document = Document(source=SourceRef("folder", "/x"), tenant_id=TENANT, title="x", text="texto")

    result = await classifier.classify(document)

    assert result.classification == Classification.C2
    assert result.method == "default"


async def test_a_model_outage_falls_through_to_the_default() -> None:
    class BrokenGateway:
        async def complete(self, *args: Any, **kwargs: Any) -> Any:
            raise ModelGatewayError("gateway down")

    classifier = Classifier(default=Classification.C2, gateway=BrokenGateway())  # type: ignore[arg-type]
    document = Document(source=SourceRef("folder", "/x"), tenant_id=TENANT, title="x", text="texto")

    assert (await classifier.classify(document)).method == "default"


async def test_an_unknown_source_label_is_ignored_not_guessed() -> None:
    classifier = Classifier(default=Classification.C2, use_model=False)
    document = Document(
        source=SourceRef("sharepoint", "/x"),
        tenant_id=TENANT,
        title="x",
        text="texto",
        metadata={"sensitivity_label": "Etiqueta Rara"},
    )

    assert (await classifier.classify(document)).method == "default"


# ------------------------------------------------------------------ sources


class FakeS3Body:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


class FakeS3Client:
    """Enough of the aiobotocore surface for the reader's contract."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects
        self.fetched: list[str] = []

    def get_paginator(self, name: str) -> Any:
        del name
        objects = self._objects

        class Paginator:
            def paginate(self, **kwargs: Any) -> Any:
                del kwargs

                async def pages() -> AsyncIterator[dict[str, Any]]:
                    yield {
                        "Contents": [
                            {"Key": key, "ETag": f'"{hash(value) & 0xFFFF:x}"'}
                            for key, value in objects.items()
                        ]
                    }

                return pages()

        return Paginator()

    async def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        del Bucket
        self.fetched.append(Key)
        return {"Body": FakeS3Body(self._objects[Key])}

    async def __aenter__(self) -> FakeS3Client:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _s3_reader(objects: dict[str, bytes], monkeypatch: pytest.MonkeyPatch) -> Any:
    client = FakeS3Client(objects)

    class FakeSession:
        def create_client(self, *args: Any, **kwargs: Any) -> FakeS3Client:
            del args, kwargs
            return client

    reader = S3SourceReader(bucket="corpus", tenant_id=TENANT, acl_groups=["finanzas"])
    monkeypatch.setattr(reader, "_session", lambda: FakeSession())
    return reader, client


async def test_s3_reads_documents_and_skips_unparseable_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, _ = _s3_reader(
        {
            "politicas/viaticos.md": b"# Viaticos\n\nLimite 1500 MXN.",
            "imagenes/logo.png": b"\x89PNG",
            "carpeta/": b"",
        },
        monkeypatch,
    )

    documents = [d async for d in reader.read(SyncCursor())]

    assert [d.title for d in documents] == ["viaticos"]
    assert documents[0].acl.groups == frozenset({"finanzas"})
    assert documents[0].source_id == "s3://corpus/politicas/viaticos.md"


async def test_s3_skips_objects_whose_etag_has_not_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, client = _s3_reader({"a.md": b"contenido"}, monkeypatch)
    cursor = SyncCursor()

    first = [d async for d in reader.read(cursor)]
    second = [d async for d in reader.read(cursor)]

    assert len(first) == 1
    assert second == []
    assert client.fetched == ["a.md"], "an unchanged object must not be downloaded twice"


async def test_s3_without_the_extra_reports_a_clear_error() -> None:
    reader = S3SourceReader(bucket="corpus", tenant_id=TENANT)
    if _has("aiobotocore"):
        pytest.skip("the `sources` extra is installed in this environment")

    with pytest.raises(SourceError, match="sources"):
        _ = [d async for d in reader.read(SyncCursor())]


async def test_sharepoint_without_credentials_refuses_to_start() -> None:
    reader = SharePointSourceReader(
        site="https://acme.sharepoint.com/sites/finanzas",
        drives=["Documentos"],
        tenant_id=TENANT,
        graph_tenant="",
        client_id="",
        client_secret="",
    )

    assert await reader.health() is False
    with pytest.raises(SourceError):
        _ = [d async for d in reader.read(SyncCursor())]


def test_sharepoint_falls_back_to_the_profile_acl_when_the_item_has_none() -> None:
    """Never an empty ACL: a document whose permissions did not resolve is not public."""
    reader = SharePointSourceReader(
        site="s",
        drives=["d"],
        tenant_id=TENANT,
        graph_tenant="t",
        client_id="c",
        client_secret="s",
        fallback_groups=["finanzas"],
    )

    class Item:
        permissions = None

    acl = reader._acl_for(Item())

    assert acl.groups == frozenset({"finanzas"})


def test_build_reader_rejects_an_unknown_source_type() -> None:
    class Unknown:
        type = "carrier-pigeon"
        default_classification = None

    with pytest.raises(SourceError, match="unknown source"):
        build_reader(Unknown(), tenant_id=TENANT, env={}, default=Classification.C2)


def test_build_reader_builds_a_folder_reader_from_a_profile_entry() -> None:
    profile = load_profile(
        REPO_ROOT / "configs" / "agent.profile.example.yaml",
        env={
            "MCP_GATEWAY_URL": "http://gw",
            "N8N_URL": "http://n8n",
            "GOVERNANCE_PDP_URL": "http://opa",
            "NATS_URL": "nats://n",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://o",
        },
    )
    folder = next(s for s in profile.knowledge.sources if s.type == "folder")

    reader = build_reader(folder, tenant_id=TENANT, env={}, default=Classification.C2)

    assert reader.kind == "folder"


# -------------------------------------------------------------------- stores


def test_build_vector_store_falls_back_to_memory_without_a_url() -> None:
    assert isinstance(build_vector_store(""), InMemoryVectorStore)


def test_build_graph_store_returns_none_when_graphrag_is_disabled() -> None:
    assert build_graph_store("bolt://x", "neo4j", "pw", enabled=False) is None


def test_build_graph_store_falls_back_to_memory_without_credentials() -> None:
    assert isinstance(build_graph_store("", "", ""), InMemoryGraphStore)


def test_the_qdrant_store_reports_a_missing_extra_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing optional extra must produce an actionable message, not an ImportError."""
    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("qdrant_client"):
            raise ImportError("blocked for the test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    with pytest.raises(VectorStoreError, match="knowledge"):
        QdrantVectorStore("http://localhost:6333")


def test_the_neo4j_store_reports_a_missing_extra_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("neo4j"):
            raise ImportError("blocked for the test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    with pytest.raises(GraphStoreError, match="knowledge"):
        Neo4jGraphStore("bolt://localhost:7687", "neo4j", "pw")


# ------------------------------------------------------------------- graph


async def test_the_graph_forgets_a_single_source() -> None:
    graph = InMemoryGraphStore()
    for name in ("uno", "dos"):
        chunk = Chunk(
            chunk_id="p0",
            source_id=f"folder:///data/{name}.md",
            tenant_id=TENANT,
            text="El ERP de SAP gestiona el proceso.",
            acl=AccessControl(groups=frozenset({"finanzas"})),
            classification=Classification.C1,
        )
        entities, relations = extract_entities(chunk.text)
        await graph.upsert(TENANT, chunk, entities, relations)

    removed = await graph.delete_source(TENANT, "folder:///data/uno.md")

    assert removed == 1
    assert await graph.health() is True


async def test_a_query_with_no_recognisable_entity_returns_nothing() -> None:
    graph = InMemoryGraphStore()

    from agent_forge.core.state import Identity
    from agent_forge.knowledge import build_filter

    identity = Identity(
        tenant_id=TENANT,
        user_id="u",
        groups=("finanzas",),
        classification_ceiling=Classification.C2,
        authenticated=True,
    )

    assert await graph.search("y de eso que", build_filter(identity)) == []


# ------------------------------------------------------------------ wiring


def test_build_knowledge_assembles_the_layer_from_the_profile() -> None:
    profile = load_profile(
        REPO_ROOT / "configs" / "agent.profile.example.yaml",
        env={
            "MCP_GATEWAY_URL": "http://gw",
            "N8N_URL": "http://n8n",
            "GOVERNANCE_PDP_URL": "http://opa",
            "NATS_URL": "nats://n",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://o",
        },
    )

    service = build_knowledge(profile=profile, gateway=None, env={})

    assert service.enabled is True
    assert service.top_k == profile.knowledge.rag.top_k
    assert service.cag.enabled is profile.knowledge.cag.enabled
    assert isinstance(service.vector_store, InMemoryVectorStore)
    assert isinstance(service.graph_store, InMemoryGraphStore)


async def test_a_disabled_service_retrieves_nothing() -> None:
    from agent_forge.core.state import Identity

    profile = load_profile(
        REPO_ROOT / "configs" / "agent.profile.example.yaml",
        env={
            "MCP_GATEWAY_URL": "http://gw",
            "N8N_URL": "http://n8n",
            "GOVERNANCE_PDP_URL": "http://opa",
            "NATS_URL": "nats://n",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://o",
        },
    )
    service = build_knowledge(profile=profile, gateway=None, env={})
    service.enabled = False

    identity = Identity(tenant_id=TENANT, user_id="u", groups=("finanzas",), authenticated=True)

    assert await service.retrieve("cualquier cosa", identity) == []
    assert await service.health() is True
    await service.aclose()


def _has(module: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(module) is not None
