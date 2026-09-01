"""The inbound half of the n8n integration.

A workflow that finishes reports back here, and the correlation id resolves the waiting
tool call, which resumes the graph from where it paused.

Authentication is the tenant API key, not a shared webhook secret: a callback carries the
result of an action taken in a tenant's name, and it must be attributable to that tenant.
A callback whose correlation id nobody is waiting for is recorded rather than discarded --
the waiter timing out does not mean the workflow did not run.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from agent_forge.channels.openai_api import get_runtime
from agent_forge.connectors.n8n import N8nConnector
from agent_forge.core.errors import AuthenticationError, TaskNotFoundError
from agent_forge.observability.logging import get_logger
from agent_forge.runtime import Runtime

log = get_logger(__name__)

router = APIRouter(prefix="/channels/n8n", tags=["n8n"])


class CallbackPayload(BaseModel):
    """What an n8n workflow posts back when it finishes."""

    correlation_id: str = Field(min_length=1, max_length=200)
    status: str = "completed"
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


def _tenant_for(runtime: Runtime, authorization: str | None, api_key: str | None) -> str:
    """Resolve the tenant from a credential. No credential, no callback."""
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    tenant = runtime.authenticator.tenant_for_api_key(api_key or token)
    if tenant is None:
        raise AuthenticationError("callback requires a valid tenant API key")
    return tenant


@router.post("/callback")
async def n8n_callback(
    body: CallbackPayload,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Resolve a pending workflow call and let the graph continue."""
    tenant = _tenant_for(runtime, authorization, x_api_key)
    if tenant != runtime.tenant_id:
        raise AuthenticationError("callback credential belongs to another tenant")

    connector = _n8n_connector(runtime)
    payload: dict[str, Any] = {"status": body.status, "output": body.output}
    if body.error:
        payload["error"] = body.error

    delivered = connector.deliver_callback(body.correlation_id, payload)
    log.info(
        "n8n.callback_received",
        correlation_id=body.correlation_id,
        status=body.status,
        delivered=delivered,
    )
    return {
        "status": "ok",
        "delivered": delivered,
        "detail": (
            "entregado al grafo"
            if delivered
            else "nadie esperaba este callback; se conserva para inspeccion"
        ),
    }


@router.get("/pending")
async def pending(runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    """How many workflow calls are waiting. Useful when a task looks stuck."""
    return dict(_n8n_connector(runtime).callbacks.stats())


def _n8n_connector(runtime: Runtime) -> N8nConnector:
    connector = next(
        (runtime.connectors.get(name) for name in runtime.connectors.names() if name == "n8n"),
        None,
    )
    if not isinstance(connector, N8nConnector):
        raise TaskNotFoundError("this cell has no n8n connector registered")
    return connector
