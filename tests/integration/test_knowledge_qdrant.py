"""The access filter against a real Qdrant.

The unit suite proves the predicate. This proves that the predicate translated into a
Qdrant filter means the same thing -- which is where a subtle mistake would live, because
a filter that is merely *wrong* returns fewer results and nobody notices, while a filter
that is too permissive returns documents nobody was allowed to see.

Requires Docker. Skipped automatically when it is not available.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_forge.core.classification import Classification
from agent_forge.core.state import Identity
from agent_forge.knowledge import AccessControl, Chunk, PolicyView, build_filter
from agent_forge.knowledge.rag import HashingEmbeddings, QdrantVectorStore

pytestmark = pytest.mark.integration

TENANT = "acme-mx"
OTHER = "otra-empresa"

LEADER = Identity(
    tenant_id=TENANT,
    user_id="lider-1",
    groups=("finanzas", "finanzas-lideres"),
    classification_ceiling=Classification.C4,
    authenticated=True,
)
ANALYST = Identity(
    tenant_id=TENANT,
    user_id="analista-1",
    groups=("finanzas",),
    classification_ceiling=Classification.C2,
    authenticated=True,
)
OUTSIDER = Identity(
    tenant_id=OTHER,
    user_id="intruso",
    groups=("finanzas", "finanzas-lideres"),
    classification_ceiling=Classification.C4,
    authenticated=True,
)


def chunk(
    text: str,
    *,
    chunk_id: str,
    groups: tuple[str, ...],
    classification: Classification,
    tenant: str = TENANT,
    users: tuple[str, ...] = (),
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        source_id=f"folder:///data/{chunk_id}.md",
        tenant_id=tenant,
        text=text,
        title=chunk_id,
        acl=AccessControl(groups=frozenset(groups), users=frozenset(users)),
        classification=classification,
    )


CORPUS = [
    chunk(
        "Limite de viaticos nacionales: 1500 MXN.",
        chunk_id="viaticos",
        groups=("finanzas",),
        classification=Classification.C2,
    ),
    chunk(
        "Sueldos del comite directivo por persona.",
        chunk_id="nomina",
        groups=("finanzas-lideres",),
        classification=Classification.C2,
    ),
    chunk(
        "Plan de adquisiciones reservado.",
        chunk_id="adquisiciones",
        groups=("finanzas",),
        classification=Classification.C4,
    ),
    chunk(
        "Datos de la otra empresa.",
        chunk_id="ajeno",
        groups=("finanzas", "finanzas-lideres"),
        classification=Classification.C0,
        tenant=OTHER,
    ),
    chunk(
        "Nota dirigida al analista por nombre.",
        chunk_id="nominativa",
        groups=(),
        classification=Classification.C1,
        users=("analista-1",),
    ),
]


@pytest.fixture(scope="module")
def qdrant_url() -> Any:
    testcontainers = pytest.importorskip("testcontainers.qdrant")
    try:
        container = testcontainers.QdrantContainer("qdrant/qdrant:v1.19.0")
        container.start()
    except Exception as exc:  # no docker here means skip, not fail
        pytest.skip(f"docker unavailable for integration tests: {exc}")
    try:
        yield f"http://{container.get_container_host_ip()}:{container.get_exposed_port(6333)}"
    finally:
        container.stop()


@pytest.fixture
async def store(qdrant_url: str) -> AsyncIterator[QdrantVectorStore]:
    pytest.importorskip("qdrant_client")
    backing = QdrantVectorStore(qdrant_url, collection="itest_knowledge")
    embeddings = HashingEmbeddings(dimension=128)
    await backing.ensure_collection(embeddings.dimension)
    await backing.delete_tenant(TENANT)
    await backing.delete_tenant(OTHER)

    vectors = await embeddings.embed([c.text for c in CORPUS])
    for item, vector in zip(CORPUS, vectors, strict=True):
        item.vector = tuple(vector)
    await backing.upsert(CORPUS)
    yield backing
    await backing.aclose()


async def _query(store: QdrantVectorStore, identity: Identity, text: str, **kwargs: Any) -> Any:
    embeddings = HashingEmbeddings(dimension=128)
    vector = await embeddings.embed_query(text)
    return await store.search(vector, build_filter(identity, **kwargs), limit=10)


async def test_the_analyst_sees_only_what_their_group_and_ceiling_allow(
    store: QdrantVectorStore,
) -> None:
    results = await _query(store, ANALYST, "informacion del area")

    seen = {r.chunk.chunk_id for r in results}
    assert "viaticos" in seen
    assert "nominativa" in seen, "a user-level ACL entry must match too"
    assert "nomina" not in seen, "wrong group"
    assert "adquisiciones" not in seen, "above the ceiling"
    assert "ajeno" not in seen, "another tenant"


async def test_group_and_user_acls_grant_independently(store: QdrantVectorStore) -> None:
    """The two visibilities overlap rather than nest, which is the point.

    The leader has the group and the clearance the analyst lacks; the analyst has a
    user-level ACL entry the leader does not. Neither set contains the other, and a
    filter that treated group membership as strictly ranking people would get this wrong.
    """
    leader = {r.chunk.chunk_id for r in await _query(store, LEADER, "informacion del area")}
    analyst = {r.chunk.chunk_id for r in await _query(store, ANALYST, "informacion del area")}

    assert {"nomina", "adquisiciones"} <= leader
    assert {"nomina", "adquisiciones"} & analyst == set()
    assert "nominativa" in analyst
    assert "nominativa" not in leader, "a user-level ACL grants that user, not their seniors"
    assert "viaticos" in leader & analyst


async def test_a_matching_group_in_another_tenant_grants_nothing(
    store: QdrantVectorStore,
) -> None:
    """Same group names, different tenant: the tenant condition is not negotiable."""
    results = await _query(store, OUTSIDER, "informacion del area")

    seen = {r.chunk.chunk_id for r in results}
    assert seen == {"ajeno"}


async def test_deny_all_returns_nothing_from_a_populated_collection(
    store: QdrantVectorStore,
) -> None:
    results = await _query(store, LEADER, "informacion", policy=PolicyView(deny_all=True))

    assert results == []


async def test_a_denied_source_is_excluded(store: QdrantVectorStore) -> None:
    policy = PolicyView(denied_sources=frozenset({"folder:///data/viaticos.md"}))

    results = await _query(store, LEADER, "limite de viaticos", policy=policy)

    assert all(r.chunk.chunk_id != "viaticos" for r in results)


async def test_scroll_applies_the_same_filter_as_search(store: QdrantVectorStore) -> None:
    """BM25 and CAG both read through scroll; a looser scroll would be a second door."""
    scrolled = {c.chunk_id for c in await store.scroll(build_filter(ANALYST), limit=100)}
    searched = {r.chunk.chunk_id for r in await _query(store, ANALYST, "informacion")}

    assert "nomina" not in scrolled
    assert "adquisiciones" not in scrolled
    assert searched <= scrolled


async def test_reingesting_a_source_replaces_its_chunks(store: QdrantVectorStore) -> None:
    before = await store.count(TENANT)

    removed = await store.delete_source(TENANT, "folder:///data/viaticos.md")

    assert removed == 1
    assert await store.count(TENANT) == before - 1


async def test_delete_tenant_leaves_the_other_tenant_intact(
    store: QdrantVectorStore,
) -> None:
    await store.delete_tenant(TENANT)

    assert await store.count(TENANT) == 0
    assert await store.count(OTHER) == 1


async def test_health_is_true_against_a_live_server(store: QdrantVectorStore) -> None:
    assert await store.health() is True
