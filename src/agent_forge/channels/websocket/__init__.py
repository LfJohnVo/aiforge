"""WebSocket channel for the cell's own UIs.

Same contract as every other channel; the difference is that authentication happens once,
in the handshake, and a connection that cannot present a credential is closed rather than
allowed to sit there anonymously.

Events are delimited by type (``token``, ``citation``, ``awaiting_approval``, ``done``,
``error``) so a UI can render provenance and a pending approval as first-class states
instead of parsing them out of prose.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from agent_forge.core.errors import AgentForgeError, AuthenticationError
from agent_forge.core.state import Identity
from agent_forge.observability.logging import get_logger
from agent_forge.runtime import Runtime

log = get_logger(__name__)

__all__ = ["router"]

router = APIRouter(tags=["websocket"])

# WebSocket close codes. 4401 mirrors HTTP 401 in the application range.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_BAD_REQUEST = 4400
MAX_MESSAGE_CHARS = 8000
# Rendering piece by piece is what makes a UI feel live; the answer itself is produced
# whole, after the quality gate has accepted it.
CHUNK_CHARS = 24


class ClientMessage(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    thread_id: str = ""

    model_config = {"extra": "ignore"}


@router.websocket("/ws/chat")
async def chat(websocket: WebSocket) -> None:
    """One conversation over one socket."""
    runtime: Runtime | None = getattr(websocket.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - only on a misbuilt app
        await websocket.close(code=CLOSE_BAD_REQUEST)
        return

    try:
        identity = _authenticate(websocket, runtime)
    except AuthenticationError as exc:
        # Closed before accepting: an unauthenticated socket never becomes a session.
        await websocket.close(code=CLOSE_UNAUTHENTICATED, reason=exc.message[:120])
        return

    await websocket.accept()
    await _send(websocket, {"type": "ready", "tenant": identity.tenant_id})
    log.info("ws.connected", tenant_id=identity.tenant_id, identified=bool(identity.user_id))

    try:
        while True:
            raw = await websocket.receive_text()
            await _handle(websocket, runtime, identity, raw)
    except WebSocketDisconnect:
        log.info("ws.disconnected", tenant_id=identity.tenant_id)


def _authenticate(websocket: WebSocket, runtime: Runtime) -> Identity:
    """Credential from the header, or from a query parameter for browser clients.

    Browsers cannot set headers on a WebSocket handshake, so the query parameter exists
    out of necessity. It is why the cell is meant to sit behind TLS: a token in a query
    string is a token in somebody's proxy log.
    """
    header = websocket.headers.get("authorization")
    api_key = websocket.headers.get("x-api-key") or websocket.query_params.get("api_key")
    return runtime.authenticator.authenticate(authorization=header, api_key_header=api_key)


async def _handle(websocket: WebSocket, runtime: Runtime, identity: Identity, raw: str) -> None:
    try:
        message = ClientMessage.model_validate_json(raw)
    except ValidationError as exc:
        await _send(
            websocket, {"type": "error", "error": "invalid message", "detail": exc.error_count()}
        )
        return

    try:
        record = await runtime.tasks.ask(message.content, identity, thread_id=message.thread_id)
    except AgentForgeError as exc:
        await _send(websocket, {"type": "error", "error": exc.code, "detail": exc.message})
        return

    for start in range(0, len(record.answer), CHUNK_CHARS):
        await _send(
            websocket,
            {"type": "token", "text": record.answer[start : start + CHUNK_CHARS]},
        )
    for citation in record.citations:
        await _send(websocket, {"type": "citation", "reference": citation})
    if str(record.state) == "input-required":
        await _send(
            websocket,
            {
                "type": "awaiting_approval",
                "task_id": record.task_id,
                "detail": "la accion espera aprobacion humana",
            },
        )
    await _send(
        websocket,
        {
            "type": "done",
            "task_id": record.task_id,
            "thread_id": record.thread_id,
            "classification": record.classification,
        },
    )


async def _send(websocket: WebSocket, payload: dict[str, Any]) -> None:
    await websocket.send_text(json.dumps(payload, ensure_ascii=False))
