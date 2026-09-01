"""Administration and operation API.

The important endpoint here is ``/admin/approvals``: it is the human end of the HITL
pause. Approving resumes the graph from the exact checkpoint where ``interrupt()`` was
called, in whatever process happens to be running at the time.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from langgraph.types import Command
from pydantic import BaseModel, Field

from agent_forge.channels.openai_api import get_runtime, resolve_identity
from agent_forge.core.checkpointer import namespaced_thread_id
from agent_forge.core.errors import ApprovalError, AuthorizationError, TaskNotFoundError
from agent_forge.core.hitl import Approver, apply_approval, apply_rejection
from agent_forge.core.state import AgentState, Identity
from agent_forge.observability.logging import get_logger
from agent_forge.runtime import Runtime

log = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=1000)


def require_authenticated(
    identity: Annotated[Identity, Depends(resolve_identity)],
) -> Identity:
    """Admin endpoints need a real user, never a bare tenant API key.

    An API key identifies a system, and a system cannot be the accountable approver of
    an A2 action.
    """
    if identity.is_anonymous:
        raise AuthorizationError(
            "administration requires an authenticated user, not a tenant API key"
        )
    return identity


@router.get("/config")
async def effective_config(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    identity: Annotated[Identity, Depends(require_authenticated)],
) -> dict[str, Any]:
    """The profile actually in force, with secrets stripped.

    Operators need to see what a cell believes it is configured as; they must not be
    able to read credentials out of it. Anything resolved from an environment variable
    is reported as a boolean "configured", never as a value.
    """
    _same_tenant(identity, runtime)
    profile = runtime.profile
    return {
        "instance": runtime.instance_key,
        "environment": runtime.settings.environment,
        "schema_version": profile.schema_version,
        "identity": {
            "agent_name": profile.identity.agent_name,
            "tenant_id": profile.identity.tenant_id,
            "area": profile.identity.area,
            "language": profile.identity.language,
        },
        "domain": {
            "subgraph": profile.domain.subgraph,
            "intents": list(runtime.deps.router.intents),
        },
        "autonomy": {
            "default": str(profile.autonomy.default),
            "overrides": {k: str(v) for k, v in profile.autonomy.overrides.items()},
        },
        "channels": profile.channels.enabled_names(),
        "upstream": {
            "orchestrator": profile.upstream.orchestrator,
            "mcp_server": profile.upstream.mcp_server.enabled,
            "a2a": profile.upstream.a2a.enabled,
        },
        "knowledge": {
            "rag": profile.knowledge.rag.enabled,
            "graphrag": profile.knowledge.graphrag.enabled,
            "cag": profile.knowledge.cag.enabled,
            "sources": [s.type for s in profile.knowledge.sources],
            "default_classification": str(profile.knowledge.default_classification),
        },
        "models": {
            "fast": profile.models.fast,
            "quality": profile.models.quality,
            "external_allowed": list(profile.models.external_allowed),
            "backends": [
                {
                    "alias": alias,
                    "sovereignty": str(runtime.policy.get(alias).sovereignty),
                    "max_classification": str(runtime.policy.get(alias).max_classification),
                }
                for alias in runtime.policy.aliases()
            ],
        },
        "governance": {
            "fail_mode": profile.governance.fail_mode,
            "pdp_configured": bool(profile.governance.pdp_url),
            "hitl_approvers_group": profile.governance.hitl_approvers_group,
            "dlp_enabled": profile.governance.dlp.enabled,
        },
        "capabilities": runtime.capabilities,
    }


@router.get("/approvals")
async def list_approvals(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    identity: Annotated[Identity, Depends(require_authenticated)],
    status: Annotated[Literal["pending", "all"], Query()] = "pending",
) -> dict[str, Any]:
    """The HITL queue for the caller's tenant."""
    _same_tenant(identity, runtime)
    pending = await runtime.approvals.list_pending(identity.tenant_id)
    del status  # only pending is materialised today; decided items live in the ledger
    return {"items": [item.to_public() for item in pending], "count": len(pending)}


@router.post("/approvals/{request_id}")
async def decide_approval(
    request_id: str,
    body: ApprovalDecision,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    identity: Annotated[Identity, Depends(require_authenticated)],
) -> Any:
    """Approve or reject a paused action, and resume the graph."""
    _same_tenant(identity, runtime)
    pending = await runtime.approvals.get(identity.tenant_id, request_id)
    if pending is None:
        raise TaskNotFoundError("no pending approval with that id", request_id=request_id)

    approver = Approver(user_id=identity.user_id or "", groups=identity.groups)
    if body.decision == "approve":
        updated = apply_approval(pending.request, approver, runtime.approval_policy)
    else:
        updated = apply_rejection(pending.request, approver, runtime.approval_policy, body.reason)
    await runtime.approvals.update(
        type(pending)(
            request=updated,
            task_id=pending.task_id,
            thread_id=pending.thread_id,
            tenant_id=pending.tenant_id,
            agent_name=pending.agent_name,
            area=pending.area,
        )
    )

    if updated.is_pending:
        # A4 needs a second, distinct approver: the action stays paused.
        return JSONResponse(
            {
                "status": "pending",
                "approvals": list(updated.approvals),
                "required_approvals": updated.required_approvals,
            }
        )

    resumed = await runtime.graph.ainvoke(
        Command(
            resume={
                "approved": updated.is_granted,
                "approver": approver.user_id,
                "reason": body.reason,
            }
        ),
        {
            "configurable": {
                "thread_id": namespaced_thread_id(
                    pending.tenant_id, runtime.settings.instance, pending.thread_id
                )
            }
        },
    )
    final = _as_state(resumed)
    log.info(
        "admin.approval_decided",
        request_id=request_id,
        decision=body.decision,
        task_id=pending.task_id,
    )
    return JSONResponse(
        {
            "status": final.status if final else "resumed",
            "granted": updated.is_granted,
            "task_id": pending.task_id,
            "answer": final.answer if final else "",
        }
    )


@router.get("/tasks/{task_id}")
async def task_status(
    task_id: str,
    thread_id: Annotated[str, Query()],
    runtime: Annotated[Runtime, Depends(get_runtime)],
    identity: Annotated[Identity, Depends(require_authenticated)],
) -> dict[str, Any]:
    """Current state of a task, read from the checkpointer."""
    _same_tenant(identity, runtime)
    config = {
        "configurable": {
            "thread_id": namespaced_thread_id(
                identity.tenant_id, runtime.settings.instance, thread_id
            )
        }
    }
    snapshot = await runtime.graph.aget_state(config)
    if snapshot is None or not snapshot.values:
        raise TaskNotFoundError("no state for that thread", task_id=task_id)
    state = AgentState.model_validate(snapshot.values)
    if state.identity.tenant_id != identity.tenant_id:
        raise TaskNotFoundError("no state for that thread", task_id=task_id)
    return {
        **state.summary(),
        "next": list(snapshot.next),
        "awaiting": [a.id for a in state.approvals if a.is_pending],
    }


@router.post("/memory/forget")
async def forget(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    identity: Annotated[Identity, Depends(require_authenticated)],
    scope: Annotated[Literal["user", "tenant"], Query()] = "user",
    subject: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Right to be forgotten. Wired to the memory layer in F2.

    Returns 501 rather than pretending: reporting a deletion that did not happen is a
    compliance failure, not a stub.
    """
    _same_tenant(identity, runtime)
    return JSONResponse(  # type: ignore[return-value]
        {
            "status": "not_implemented",
            "detail": (
                "la capa de memoria se implementa en F2; ninguna memoria persistente "
                "existe todavia en esta celula"
            ),
            "scope": scope,
            "subject": subject or identity.user_id,
        },
        status_code=501,
    )


def _same_tenant(identity: Identity, runtime: Runtime) -> None:
    """A credential for one tenant must never administer another."""
    if identity.tenant_id != runtime.tenant_id:
        raise AuthorizationError(
            "credential belongs to a different tenant than this cell",
            credential_tenant=identity.tenant_id,
        )


def _as_state(raw: Any) -> AgentState | None:
    if isinstance(raw, AgentState):
        return raw
    if isinstance(raw, dict):
        payload = {k: v for k, v in raw.items() if not k.startswith("__")}
        try:
            return AgentState.model_validate(payload)
        except ValueError:
            return None
    return None


__all__ = ["ApprovalDecision", "ApprovalError", "router"]
