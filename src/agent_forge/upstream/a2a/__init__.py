"""Agent2Agent: the cell as an A2A agent.

The Agent Card at ``/.well-known/agent.json`` advertises **skills** -- what the cell can
do for an area -- not its internal tool catalogue. That distinction is deliberate: the
card is public to any orchestrator that can reach the cell, and enumerating the tenant's
tools or knowledge sources in it would be a disclosure with no benefit.

The task lifecycle is shared with the MCP surface (``upstream/tasks.py``). The state worth
noticing is ``input-required``: it is how a HITL pause surfaces upstream, so an
orchestrator can show its own human the approval instead of the task appearing to hang.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from agent_forge import __version__
from agent_forge.channels.openai_api import get_runtime, resolve_identity
from agent_forge.core.errors import AgentForgeError
from agent_forge.core.state import Identity
from agent_forge.observability.logging import get_logger
from agent_forge.runtime import Runtime
from agent_forge.upstream.tasks import TaskState

log = get_logger(__name__)

__all__ = ["agent_card", "router"]

router = APIRouter(tags=["a2a"])

PROTOCOL_VERSION = "0.3.0"


class MessagePart(BaseModel):
    kind: str = "text"
    text: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class A2AMessage(BaseModel):
    role: str = "user"
    parts: list[MessagePart] = Field(default_factory=list)
    messageId: str = ""  # noqa: N815 - the A2A wire format is camelCase
    contextId: str = ""  # noqa: N815

    def text(self) -> str:
        return " ".join(part.text for part in self.parts if part.kind == "text").strip()


class SendParams(BaseModel):
    message: A2AMessage
    blocking: bool = True


class JsonRpcRequest(BaseModel):
    """A2A rides on JSON-RPC 2.0."""

    jsonrpc: str = "2.0"
    id: str | int = 1
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


def agent_card(runtime: Runtime, base_url: str = "") -> dict[str, Any]:
    """The Agent Card.

    Skills, not tools. A skill is a capability the cell offers; the tool catalogue is an
    internal detail whose disclosure would tell a caller which systems the tenant runs.
    """
    profile = runtime.profile
    url = (base_url or "").rstrip("/")
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": profile.identity.agent_name,
        "description": (
            f"Celula de agente del area {profile.identity.area}. "
            "Responde con citas cuando se apoya en documentacion del area."
        ),
        "version": __version__,
        "url": f"{url}/a2a" if url else "/a2a",
        "preferredTransport": "JSONRPC",
        "capabilities": {
            # No push notifications: the cell answers, it does not call back. Claiming
            # otherwise would have orchestrators waiting for a webhook that never fires.
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": _skills(profile),
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}},
        "security": [{"bearer": []}],
        "provider": {
            "organization": "Silent4Business",
            "url": "https://silent4business.com",
        },
    }


def _skills(profile: Any) -> list[dict[str, Any]]:
    area = profile.identity.area
    skills: list[dict[str, Any]] = [
        {
            "id": "ask",
            "name": f"Consulta al area {area}",
            "description": (
                f"Responde preguntas del area {area}. Cita sus fuentes cuando se apoya "
                "en documentacion."
            ),
            "tags": [area, "qa"],
            "examples": [f"Que dice la politica de {area}?"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
        }
    ]
    if profile.knowledge.rag.enabled:
        skills.append(
            {
                "id": "search_knowledge",
                "name": "Busqueda en la documentacion del area",
                "description": (
                    "Devuelve fragmentos relevantes con su cita, filtrados por la "
                    "identidad del solicitante. No sintetiza."
                ),
                "tags": [area, "rag"],
                "inputModes": ["text/plain"],
                "outputModes": ["application/json"],
            }
        )
    # Only advertised when the profile actually declares actions that need approval:
    # promising a HITL flow a cell cannot perform is worse than not offering it.
    if any(
        level.requires_approval
        for level in [profile.autonomy.default, *profile.autonomy.overrides.values()]
    ):
        skills.append(
            {
                "id": "run_task",
                "name": "Tarea con posible aprobacion humana",
                "description": (
                    "Ejecuta una tarea del area. Si la accion supera el nivel de "
                    "autonomia, la tarea queda en `input-required` hasta que una "
                    "persona la apruebe."
                ),
                "tags": [area, "hitl"],
                "inputModes": ["text/plain"],
                "outputModes": ["application/json"],
            }
        )
    return skills


@router.get("/.well-known/agent.json")
async def well_known_agent_card(
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """The only unauthenticated endpoint besides liveness.

    It has to be: discovery precedes authentication. It therefore contains nothing an
    anonymous reader should not see.
    """
    return agent_card(runtime, runtime.settings.public_url)


@router.get("/.well-known/agent-card.json")
async def well_known_agent_card_alias(
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    """The newer spelling of the same document, served identically."""
    return agent_card(runtime, runtime.settings.public_url)


@router.post("/a2a")
async def a2a_rpc(
    request: JsonRpcRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    identity: Annotated[Identity, Depends(resolve_identity)],
) -> dict[str, Any]:
    """The JSON-RPC entry point: ``message/send``, ``tasks/get``, ``tasks/cancel``."""
    try:
        result = await _dispatch(request, runtime, identity)
    except AgentForgeError as exc:
        log.warning("a2a.error", method=request.method, code=exc.code)
        return _error(request.id, -32000, exc.message, exc.code)
    except ValueError as exc:
        return _error(request.id, -32602, str(exc), "invalid_params")
    return {"jsonrpc": "2.0", "id": request.id, "result": result}


async def _dispatch(
    request: JsonRpcRequest, runtime: Runtime, identity: Identity
) -> dict[str, Any]:
    method = request.method
    if method == "message/send":
        params = SendParams.model_validate(request.params)
        question = params.message.text()
        if not question:
            raise ValueError("the message carries no text part")
        record = (
            await runtime.tasks.ask(question, identity, thread_id=params.message.contextId)
            if params.blocking
            else await runtime.tasks.submit(question, identity, thread_id=params.message.contextId)
        )
        return record.to_a2a()

    if method in {"tasks/get", "tasks/cancel"}:
        task_id = str(request.params.get("id") or "")
        if not task_id:
            raise ValueError("tasks/get and tasks/cancel need an `id`")
        record = (
            await runtime.tasks.status(identity.tenant_id, task_id)
            if method == "tasks/get"
            else await runtime.tasks.cancel(identity.tenant_id, task_id)
        )
        return record.to_a2a()

    raise ValueError(f"unsupported method {method!r}")


def _error(request_id: str | int, code: int, message: str, data: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message, "data": data},
    }


def is_terminal(state: str) -> bool:
    """Whether an A2A state means the orchestrator can stop polling."""
    try:
        return TaskState(state).is_terminal
    except ValueError:
        return False
