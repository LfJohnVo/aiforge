"""The cell published as an MCP server.

A higher-level orchestrator -- Copilot Studio, Bedrock AgentCore, a supervising model in
vLLM -- consumes the cell the same way the cell consumes the platform's gateway.

The rule that matters here: **an orchestrator does not inherit privileges.** The identity
of the end user travels in the call, and if the orchestrator does not propagate one, the
ceiling is C0. A supervising agent is a caller, not an administrator.

Mounted as streamable HTTP inside the FastAPI app so there is one process, one port and
one authentication story.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agent_forge.core.classification import Classification
from agent_forge.core.errors import AgentForgeError
from agent_forge.core.state import Identity
from agent_forge.observability.logging import get_logger
from agent_forge.upstream.tasks import TaskRunner

log = get_logger(__name__)

__all__ = ["MAX_SEARCH_RESULTS", "TOOL_NAMES", "build_mcp_server", "identity_from_arguments"]

MAX_SEARCH_RESULTS = 10

# The five tools RF-03 requires. Named here so the A2A skills and the documentation can
# be derived from one list rather than repeating it.
TOOL_NAMES: tuple[str, ...] = (
    "ask",
    "run_task",
    "get_status",
    "search_knowledge",
    "repo_graph.query",
)


def identity_from_arguments(
    tenant_id: str, raw: Any, *, max_ceiling: Classification = Classification.C4
) -> Identity:
    """Build the requester's identity from what the orchestrator propagated.

    No propagated user means an anonymous requester, which means C0. That is the rule
    that stops a supervising agent from becoming a way around access control.
    """
    payload = raw if isinstance(raw, dict) else {}
    user_id = str(payload.get("user_id") or "").strip()
    if not user_id:
        return Identity(tenant_id=tenant_id, authenticated=False)

    groups = payload.get("groups")
    ceiling = payload.get("classification_ceiling")
    try:
        declared = Classification.parse(ceiling) if ceiling is not None else Classification.C2
    except (TypeError, ValueError):
        declared = Classification.C0
    return Identity(
        tenant_id=tenant_id,
        user_id=user_id,
        groups=tuple(str(g) for g in groups) if isinstance(groups, list) else (),
        classification_ceiling=min(declared, max_ceiling),
        authenticated=True,
    )


def build_mcp_server(
    *,
    runner: TaskRunner,
    agent_name: str,
    area: str,
    tenant_id: str,
    retrieve: Any | None = None,
    repo_graph: Any | None = None,
) -> Any:
    """Construct the MCP server exposing this cell's five tools.

    Imported lazily inside so that a profile with ``upstream.mcp_server`` disabled never
    loads the server machinery.
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name=agent_name or "agent-forge",
        instructions=(
            f"Celula de agente del area {area}. Responde con citas cuando usa "
            "documentacion. Propaga la identidad del usuario final en `requester` o "
            "la respuesta quedara limitada a contenido publico (C0)."
        ),
    )

    @server.tool()
    async def ask(question: str, requester: dict[str, Any] | None = None) -> dict[str, Any]:
        """Pregunta sincrona. Devuelve la respuesta y sus citas."""
        identity = identity_from_arguments(tenant_id, requester)
        record = await runner.ask(question, identity)
        return {
            "answer": record.answer,
            "citations": list(record.citations),
            "classification": record.classification,
            "task_id": record.task_id,
            "state": str(record.state),
        }

    @server.tool()
    async def run_task(
        question: str,
        requester: dict[str, Any] | None = None,
        thread_id: str = "",
    ) -> dict[str, Any]:
        """Lanza una tarea asincrona y devuelve su identificador."""
        identity = identity_from_arguments(tenant_id, requester)
        record = await runner.submit(question, identity, thread_id=thread_id)
        return {
            "task_id": record.task_id,
            "thread_id": record.thread_id,
            "state": str(record.state),
            "poll": "usa get_status con este task_id",
        }

    @server.tool()
    async def get_status(task_id: str) -> dict[str, Any]:
        """Estado de una tarea. `input-required` significa que espera a una persona."""
        try:
            record = await runner.status(tenant_id, task_id)
        except AgentForgeError as exc:
            return {"error": exc.message, "task_id": task_id}
        return record.to_dict()

    @server.tool()
    async def search_knowledge(
        query: str,
        requester: dict[str, Any] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Recupera fragmentos del area sin sintetizar. Filtrado por identidad."""
        if retrieve is None:
            return {"results": [], "detail": "esta celula no tiene conocimiento activo"}
        identity = identity_from_arguments(tenant_id, requester)
        found = await retrieve(query, identity, min(limit, MAX_SEARCH_RESULTS))
        return {
            "results": [
                {
                    "text": item.text,
                    "citation": item.citation.reference,
                    "score": round(item.score, 4),
                }
                for item in found
            ]
        }

    @server.tool(name="repo_graph_query")
    async def repo_graph_query(kind: str = "overview", target: str = "") -> dict[str, Any]:
        """Consulta el grafo del propio repositorio: find, dependents, dependencies, overview."""
        if repo_graph is None:
            return {"error": "el grafo del repositorio no esta disponible"}
        result = await repo_graph(kind, target)
        return dict(result) if isinstance(result, dict) else {"result": result}

    log.info("mcp_server.built", agent=agent_name, tools=list(TOOL_NAMES))
    return server


def describe_tools() -> Sequence[dict[str, str]]:
    """One description per tool, reused by the A2A Agent Card and the docs."""
    return (
        {"name": "ask", "description": "Pregunta sincrona con respuesta y citas"},
        {"name": "run_task", "description": "Tarea asincrona; devuelve un task_id"},
        {"name": "get_status", "description": "Estado de una tarea por task_id"},
        {"name": "search_knowledge", "description": "Recuperacion sin sintesis"},
        {"name": "repo_graph.query", "description": "Consulta sobre el propio repositorio"},
    )
