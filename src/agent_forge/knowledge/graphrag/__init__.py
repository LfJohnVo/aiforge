"""GraphRAG: entities, relations and neighbourhood retrieval.

Vector search answers "what text looks like this question". A graph answers "what is
connected to what", which is the question an area actually asks about its own domain --
which supplier, which system, which policy touches which process.

Access control is enforced on the graph too. Chunk nodes carry the same `tenant_id`,
`acl_groups` and `classification` as their vector counterparts, and the same
``AccessFilter`` predicate decides visibility. A graph traversal that ignored the ACL
would be a second, unguarded door into the same corpus.

Two implementations behind ``GraphStore``: Neo4j (production, extra ``knowledge``) and an
in-process one that satisfies the same contract for development and tests.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from agent_forge.core.errors import AgentForgeError
from agent_forge.knowledge.access_control import AccessFilter
from agent_forge.knowledge.documents import AccessControl, Chunk, RetrievalResult
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "Entity",
    "GraphStore",
    "GraphStoreError",
    "InMemoryGraphStore",
    "Neo4jGraphStore",
    "Relation",
    "build_graph_store",
    "extract_entities",
]


class GraphStoreError(AgentForgeError):
    """The graph store is unreachable or rejected an operation."""

    code = "graph_store_error"


@dataclass(frozen=True, slots=True)
class Entity:
    """A named thing mentioned in the corpus."""

    name: str
    kind: str = "concept"  # concept | system | org | person | policy

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name.casefold()}"


@dataclass(frozen=True, slots=True)
class Relation:
    """A co-occurrence edge between two entities, weighted by how often it appears."""

    source: Entity
    target: Entity
    kind: str = "CO_OCCURS"
    weight: float = 1.0


@runtime_checkable
class GraphStore(Protocol):
    """Storage and traversal for the knowledge graph."""

    async def upsert(
        self,
        tenant_id: str,
        chunk: Chunk,
        entities: Sequence[Entity],
        relations: Sequence[Relation],
    ) -> None: ...

    async def search(
        self, query: str, access: AccessFilter, *, limit: int = 10
    ) -> list[RetrievalResult]: ...

    async def delete_source(self, tenant_id: str, source_id: str) -> int: ...

    async def delete_tenant(self, tenant_id: str) -> int: ...

    async def health(self) -> bool: ...

    async def aclose(self) -> None: ...


# ------------------------------------------------------------------- extraction

# Deliberately literal. An LLM extractor is better at recall but non-deterministic, and a
# graph that changes shape between two ingestions of the same corpus is one nobody trusts.
# The profile can add domain vocabulary; that is where recall is bought back.
_PROPER = re.compile(r"\b(?:[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ.-]{2,}(?:\s+(?:de|del|la|y)\s+)?)+\b")
_ACRONYM = re.compile(r"\b[A-Z]{2,6}\b")
_SYSTEMS = re.compile(
    r"\b(erp|crm|siem|edr|sap|salesforce|jira|sharepoint|azure|aws|office\s?365|"
    r"active\s?directory|vpn|firewall)\b",
    re.IGNORECASE,
)
_STOP_ENTITIES = frozenset(
    {
        "el",
        "la",
        "los",
        "las",
        "un",
        "una",
        "este",
        "esta",
        "para",
        "por",
        "con",
        "sin",
        "the",
        "and",
        "for",
        "with",
        "pdf",
        "doc",
        "http",
        "https",
    }
)


def extract_entities(
    text: str, *, vocabulary: Iterable[str] = (), limit: int = 25
) -> tuple[list[Entity], list[Relation]]:
    """Pull entities and co-occurrence relations out of one chunk.

    Relations are co-occurrence within the chunk. That is a weak signal per pair and a
    useful one in aggregate, and it needs no model, so the graph is reproducible.
    """
    found: dict[str, Entity] = {}

    for match in _SYSTEMS.finditer(text):
        entity = Entity(name=match.group(0).strip().lower(), kind="system")
        found[entity.key] = entity

    for term in vocabulary:
        if term and term.casefold() in text.casefold():
            entity = Entity(name=term, kind="concept")
            found[entity.key] = entity

    for match in _PROPER.finditer(text):
        name = match.group(0).strip(" .,;:")
        if len(name) < 3 or name.casefold() in _STOP_ENTITIES:
            continue
        found.setdefault(Entity(name=name, kind="org").key, Entity(name=name, kind="org"))

    for match in _ACRONYM.finditer(text):
        name = match.group(0)
        if name.casefold() in _STOP_ENTITIES:
            continue
        found.setdefault(Entity(name=name, kind="concept").key, Entity(name=name, kind="concept"))

    entities = list(found.values())[:limit]
    relations = [
        Relation(source=a, target=b)
        for index, a in enumerate(entities)
        for b in entities[index + 1 :]
    ]
    return entities, relations


# ------------------------------------------------------------------ in-memory


@dataclass
class _Node:
    chunk: Chunk
    entities: set[str] = field(default_factory=set)


class InMemoryGraphStore:
    """Adjacency in a dict. Same contract as Neo4j, no server."""

    def __init__(self) -> None:
        self._nodes: dict[str, _Node] = {}
        self._by_entity: dict[str, set[str]] = {}
        self._edges: dict[str, dict[str, float]] = {}
        self._vocabulary: set[str] = set()

    async def upsert(
        self,
        tenant_id: str,
        chunk: Chunk,
        entities: Sequence[Entity],
        relations: Sequence[Relation],
    ) -> None:
        del tenant_id  # already on the chunk; the signature keeps the contract explicit
        node = _Node(chunk=chunk, entities={e.key for e in entities})
        self._nodes[chunk.point_id] = node
        for entity in entities:
            self._by_entity.setdefault(entity.key, set()).add(chunk.point_id)
            self._vocabulary.add(entity.name.casefold())
        for relation in relations:
            self._edges.setdefault(relation.source.key, {})
            self._edges[relation.source.key][relation.target.key] = (
                self._edges[relation.source.key].get(relation.target.key, 0.0) + relation.weight
            )
            self._edges.setdefault(relation.target.key, {})
            self._edges[relation.target.key][relation.source.key] = (
                self._edges[relation.target.key].get(relation.source.key, 0.0) + relation.weight
            )

    async def search(
        self, query: str, access: AccessFilter, *, limit: int = 10
    ) -> list[RetrievalResult]:
        """Chunks mentioning the query's entities, plus their graph neighbours."""
        seeds, _ = extract_entities(query, vocabulary=self._vocabulary)
        seed_keys = {e.key for e in seeds}
        if not seed_keys:
            return []

        # One hop out. Two hops on a co-occurrence graph returns most of the corpus.
        neighbours: dict[str, float] = dict.fromkeys(seed_keys, 1.0)
        for key in seed_keys:
            for neighbour, weight in self._edges.get(key, {}).items():
                neighbours[neighbour] = max(neighbours.get(neighbour, 0.0), 0.5 * min(weight, 2.0))

        scored: dict[str, float] = {}
        for entity_key, weight in neighbours.items():
            for point_id in self._by_entity.get(entity_key, set()):
                scored[point_id] = scored.get(point_id, 0.0) + weight

        results = [
            RetrievalResult(chunk=self._nodes[pid].chunk, score=score, retriever="graph")
            for pid, score in scored.items()
            # Same predicate as the vector store: the graph is not a second door.
            if pid in self._nodes and access.permits(self._nodes[pid].chunk)
        ]
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    async def delete_source(self, tenant_id: str, source_id: str) -> int:
        doomed = [
            pid
            for pid, node in self._nodes.items()
            if node.chunk.tenant_id == tenant_id and node.chunk.source_id == source_id
        ]
        self._drop(doomed)
        return len(doomed)

    async def delete_tenant(self, tenant_id: str) -> int:
        doomed = [pid for pid, node in self._nodes.items() if node.chunk.tenant_id == tenant_id]
        self._drop(doomed)
        return len(doomed)

    def _drop(self, point_ids: Sequence[str]) -> None:
        for pid in point_ids:
            self._nodes.pop(pid, None)
        for members in self._by_entity.values():
            members.difference_update(point_ids)

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        self._nodes.clear()
        self._by_entity.clear()
        self._edges.clear()


# ----------------------------------------------------------------------- Neo4j


class Neo4jGraphStore:
    """Neo4j adapter. Every node and relation carries ``tenant_id``."""

    def __init__(self, uri: str, user: str, password: str, *, database: str = "neo4j") -> None:
        try:
            from neo4j import AsyncGraphDatabase
        except ImportError as exc:  # pragma: no cover - guarded by capability check
            raise GraphStoreError(
                "the `knowledge` extra is required for the Neo4j graph store",
                hint="uv sync --extra knowledge",
            ) from exc
        self._driver: Any = AsyncGraphDatabase.driver(uri, auth=(user, password))
        self._database = database
        self._prepared = False

    async def _prepare(self) -> None:
        if self._prepared:
            return
        statements = [
            "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.point_id IS UNIQUE",
            "CREATE CONSTRAINT entity_key IF NOT EXISTS FOR (e:Entity) REQUIRE e.key IS UNIQUE",
            "CREATE INDEX chunk_tenant IF NOT EXISTS FOR (c:Chunk) ON (c.tenant_id)",
            "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
        ]
        async with self._driver.session(database=self._database) as session:
            for statement in statements:
                await session.run(statement)
        self._prepared = True

    async def upsert(
        self,
        tenant_id: str,
        chunk: Chunk,
        entities: Sequence[Entity],
        relations: Sequence[Relation],
    ) -> None:
        await self._prepare()
        payload = {
            "tenant_id": tenant_id,
            "point_id": chunk.point_id,
            "chunk_id": chunk.chunk_id,
            "source_id": chunk.source_id,
            "title": chunk.title,
            "text": chunk.text,
            "classification": int(chunk.classification),
            "acl_groups": sorted(chunk.acl.groups),
            "acl_users": sorted(chunk.acl.users),
            "entities": [{"key": e.key, "name": e.name, "kind": e.kind} for e in entities],
            "relations": [
                {"a": r.source.key, "b": r.target.key, "weight": r.weight} for r in relations
            ],
        }
        query = """
        MERGE (c:Chunk {point_id: $point_id})
        SET c.tenant_id = $tenant_id, c.chunk_id = $chunk_id, c.source_id = $source_id,
            c.title = $title, c.text = $text, c.classification = $classification,
            c.acl_groups = $acl_groups, c.acl_users = $acl_users
        WITH c
        UNWIND $entities AS entity
          MERGE (e:Entity {key: entity.key, tenant_id: $tenant_id})
          SET e.name = entity.name, e.kind = entity.kind
          MERGE (e)-[:MENTIONED_IN]->(c)
        WITH c
        UNWIND $relations AS rel
          MATCH (a:Entity {key: rel.a, tenant_id: $tenant_id})
          MATCH (b:Entity {key: rel.b, tenant_id: $tenant_id})
          MERGE (a)-[r:CO_OCCURS]-(b)
          SET r.weight = coalesce(r.weight, 0) + rel.weight
        """
        try:
            async with self._driver.session(database=self._database) as session:
                await session.run(query, **payload)
        except Exception as exc:
            raise GraphStoreError("neo4j upsert failed", detail=str(exc)) from exc

    async def search(
        self, query: str, access: AccessFilter, *, limit: int = 10
    ) -> list[RetrievalResult]:
        if access.allow_nothing:
            return []
        seeds, _ = extract_entities(query)
        if not seeds:
            return []
        await self._prepare()

        # The ACL is part of the MATCH, not a post-filter, for the same reason as in the
        # vector store: a forbidden chunk must not occupy a result slot.
        cypher = """
        MATCH (seed:Entity)-[:CO_OCCURS*0..1]-(near:Entity)-[:MENTIONED_IN]->(c:Chunk)
        WHERE seed.key IN $seeds
          AND c.tenant_id = $tenant_id
          AND c.classification <= $ceiling
          AND NOT c.source_id IN $denied
          AND (any(g IN c.acl_groups WHERE g IN $groups)
               OR any(u IN c.acl_users WHERE u = $user_id))
        RETURN c, count(DISTINCT near) AS hits
        ORDER BY hits DESC LIMIT $limit
        """
        params = {
            "seeds": [e.key for e in seeds],
            "tenant_id": access.tenant_id,
            "ceiling": int(access.ceiling),
            "groups": sorted(access.groups),
            "user_id": access.user_id,
            "denied": sorted(access.denied_sources),
            "limit": limit,
        }
        try:
            async with self._driver.session(database=self._database) as session:
                cursor = await session.run(cypher, **params)
                records = [record async for record in cursor]
        except Exception as exc:
            raise GraphStoreError("neo4j search failed", detail=str(exc)) from exc

        return [_result_from_record(record) for record in records]

    async def delete_source(self, tenant_id: str, source_id: str) -> int:
        return await self._delete(
            "MATCH (c:Chunk {tenant_id: $tenant_id, source_id: $source_id}) "
            "DETACH DELETE c RETURN count(c) AS removed",
            {"tenant_id": tenant_id, "source_id": source_id},
        )

    async def delete_tenant(self, tenant_id: str) -> int:
        return await self._delete(
            "MATCH (n) WHERE n.tenant_id = $tenant_id DETACH DELETE n RETURN count(n) AS removed",
            {"tenant_id": tenant_id},
        )

    async def _delete(self, cypher: str, params: dict[str, Any]) -> int:
        try:
            async with self._driver.session(database=self._database) as session:
                cursor = await session.run(cypher, **params)
                record = await cursor.single()
        except Exception as exc:
            raise GraphStoreError("neo4j delete failed", detail=str(exc)) from exc
        return int(record["removed"]) if record else 0

    async def health(self) -> bool:
        try:
            await self._driver.verify_connectivity()
        except Exception:
            return False
        return True

    async def aclose(self) -> None:
        await self._driver.close()


def _result_from_record(record: Any) -> RetrievalResult:
    node = record["c"]
    chunk = Chunk(
        chunk_id=str(node.get("chunk_id", "")),
        source_id=str(node.get("source_id", "")),
        tenant_id=str(node.get("tenant_id", "")),
        text=str(node.get("text", "")),
        title=str(node.get("title", "")),
        acl=AccessControl(
            groups=frozenset(node.get("acl_groups") or []),
            users=frozenset(node.get("acl_users") or []),
        ),
    )
    from agent_forge.core.classification import Classification

    chunk.classification = Classification(int(node.get("classification", 2)))
    return RetrievalResult(chunk=chunk, score=float(record["hits"]), retriever="graph")


def build_graph_store(
    uri: str = "",
    user: str = "",
    password: str = "",
    *,
    enabled: bool = True,
    strict: bool = False,
) -> GraphStore | None:
    """Neo4j when configured, enabled and installed; in-process otherwise; None when off.

    ``strict`` mirrors ``check_capabilities``: fatal in production, a degradation in
    development. Same reasoning as the vector store -- the API image carries no
    `knowledge` extra by design (ADR-005), and without this the `core` profile cannot boot
    against a profile with GraphRAG enabled.
    """
    if not enabled:
        return None
    if uri and password:
        try:
            return Neo4jGraphStore(uri, user or "neo4j", password)
        except GraphStoreError:
            if strict:
                raise
            log.warning(
                "graph_store.extra_missing",
                detail=(
                    "NEO4J_URI is set but the `knowledge` extra is not installed; "
                    "falling back to the in-process graph. Nothing is written to Neo4j."
                ),
            )
            return InMemoryGraphStore()
    log.warning(
        "graph_store.in_memory_selected",
        detail="NEO4J_URI/NEO4J_PASSWORD not set; the graph is lost on restart",
    )
    return InMemoryGraphStore()
