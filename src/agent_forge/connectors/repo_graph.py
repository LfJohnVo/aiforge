"""``repo_graph.query``: the agent answering questions about its own repository.

The runtime half of RF-12. ``scripts/repo_graph.py`` builds the graph from the AST;
this reads it and answers four questions an engineer actually asks:

* where does a symbol live,
* what would break if I changed this module,
* what does this repository depend on,
* what is the shape of the thing.

Always A0 and always C1: the graph contains no tenant data, only the structure of the
code that is already open in the developer's editor. It is in the registry's builtin
allowlist for the same reason.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent_forge.connectors.base import (
    CallContext,
    ConnectorBase,
    ToolResult,
    ToolSpec,
)
from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import Classification
from agent_forge.core.errors import ToolError
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = ["RepoGraphConnector"]

DEFAULT_GRAPH = Path("docs/graphs/repo-graph.json")
MAX_RESULTS = 25

_QUERIES = {
    "find": "Localiza clases y funciones cuyo nombre contenga el texto",
    "dependents": "Modulos que importan el modulo indicado (radio de impacto de un cambio)",
    "dependencies": "Modulos y paquetes externos que importa el modulo indicado",
    "overview": "Resumen del repositorio: modulos, simbolos y los mas importados",
}


class RepoGraphConnector(ConnectorBase):
    """Reads the generated repository graph and answers structural questions."""

    def __init__(self, graph_path: str | Path = DEFAULT_GRAPH) -> None:
        super().__init__(name="repo_graph", version="1.0.0", timeout=5.0)
        self._path = Path(graph_path)
        self._graph: dict[str, Any] | None = None

    def capabilities(self) -> Sequence[ToolSpec]:
        return (
            ToolSpec(
                name="repo_graph.query",
                description=(
                    "Consulta el grafo del propio repositorio. Tipos: "
                    + ", ".join(f"{k} ({v})" for k, v in _QUERIES.items())
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": sorted(_QUERIES)},
                        "target": {
                            "type": "string",
                            "description": "simbolo o modulo, segun el tipo de consulta",
                        },
                    },
                    "required": ["kind"],
                    "additionalProperties": False,
                },
                required_scope="repo:read",
                autonomy_min=AutonomyLevel.A0,
                max_classification=Classification.C1,
                readonly=True,
            ),
        )

    async def health(self) -> bool:
        return self._path.is_file()

    def _load(self) -> dict[str, Any]:
        if self._graph is None:
            if not self._path.is_file():
                raise ToolError(
                    "the repository graph has not been generated",
                    hint="run `make repo-graph`",
                    path=str(self._path),
                )
            self._graph = json.loads(self._path.read_text(encoding="utf-8"))
        return self._graph

    async def invoke(self, tool: str, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        del ctx  # structure of our own code; no tenant scoping applies
        if tool != "repo_graph.query":
            return ToolResult.failure(f"unknown tool {tool}")

        kind = str(args.get("kind", "overview"))
        target = str(args.get("target", ""))
        if kind not in _QUERIES:
            return ToolResult.failure(
                f"unknown query kind {kind!r}; expected one of {sorted(_QUERIES)}"
            )

        async def call() -> dict[str, Any]:
            graph = self._load()
            if kind == "find":
                return self._find(graph, target)
            if kind == "dependents":
                return self._dependents(graph, target)
            if kind == "dependencies":
                return self._dependencies(graph, target)
            return self._overview(graph)

        return await self.run(tool, call, classification=Classification.C1)

    # ------------------------------------------------------------- queries

    @staticmethod
    def _find(graph: dict[str, Any], target: str) -> dict[str, Any]:
        needle = target.casefold()
        if not needle:
            raise ToolError("`find` needs a target")
        hits = [
            {
                "id": node["id"],
                "kind": node.get("kind"),
                "path": node.get("path"),
                "line": node.get("lineno"),
                "doc": node.get("doc", ""),
            }
            for node in graph.get("nodes", [])
            if node.get("kind") in {"class", "function"} and needle in node["id"].casefold()
        ]
        return {
            "query": "find",
            "target": target,
            "count": len(hits),
            "results": hits[:MAX_RESULTS],
        }

    @staticmethod
    def _dependents(graph: dict[str, Any], target: str) -> dict[str, Any]:
        if not target:
            raise ToolError("`dependents` needs a module id")
        found = sorted(
            {
                edge["source"]
                for edge in graph.get("edges", [])
                if edge.get("kind") == "imports" and edge.get("target") == target
            }
        )
        return {
            "query": "dependents",
            "target": target,
            "count": len(found),
            "results": found[:MAX_RESULTS],
            "note": "cambiar este modulo afecta a estos" if found else "nadie lo importa",
        }

    @staticmethod
    def _dependencies(graph: dict[str, Any], target: str) -> dict[str, Any]:
        if not target:
            raise ToolError("`dependencies` needs a module id")
        internal = sorted(
            {
                edge["target"]
                for edge in graph.get("edges", [])
                if edge.get("kind") == "imports" and edge.get("source") == target
            }
        )
        external = sorted(
            {
                edge["target"]
                for edge in graph.get("edges", [])
                if edge.get("kind") == "depends" and edge.get("source") == target
            }
        )
        return {
            "query": "dependencies",
            "target": target,
            "internal": internal[:MAX_RESULTS],
            "external": external[:MAX_RESULTS],
        }

    @staticmethod
    def _overview(graph: dict[str, Any]) -> dict[str, Any]:
        nodes = graph.get("nodes", [])
        edges = graph.get("edges", [])
        imported = Counter(edge["target"] for edge in edges if edge.get("kind") == "imports")
        return {
            "query": "overview",
            "modules": sum(1 for n in nodes if n.get("kind") == "module"),
            "classes": sum(1 for n in nodes if n.get("kind") == "class"),
            "functions": sum(1 for n in nodes if n.get("kind") == "function"),
            "lines_of_code": sum(int(m.get("loc", 0)) for m in graph.get("modules", [])),
            "most_imported": [
                {"module": module, "importers": count} for module, count in imported.most_common(10)
            ],
        }
