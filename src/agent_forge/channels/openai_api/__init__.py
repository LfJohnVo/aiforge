"""OpenAI-compatible chat channel.

Being wire-compatible with ``/v1/chat/completions`` is what makes OpenWebUI, LibreChat
and every other standard client work with zero adapter code -- so this is the primary
channel, not a convenience.

The channel decides nothing. It normalises the request, propagates the verified identity
and serialises whatever the graph produces. Filtering, routing and authorisation all
happen further in.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.hitl import PendingApproval
from agent_forge.core.state import AgentState, ApprovalRequest, Identity, Message
from agent_forge.observability.logging import bind_request_context, get_logger
from agent_forge.runtime import Runtime

log = get_logger(__name__)

router = APIRouter(tags=["chat"])

MODEL_ID = "agent-forge"


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    """The subset of the OpenAI schema this cell honours, plus ``x_`` extensions."""

    model: str = MODEL_ID
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    user: str | None = None
    # Extensions. A client that ignores them keeps working.
    x_thread_id: str | None = None
    x_task_id: str | None = None

    model_config = {"extra": "ignore"}


def get_runtime(request: Request) -> Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - only reachable on a misbuilt app
        raise RuntimeError("application has no runtime; build it in the lifespan")
    return runtime  # type: ignore[no-any-return]


def resolve_identity(
    request: Request,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> Identity:
    """Authenticate. Raises ``AuthenticationError``, mapped to 401 by the app handler."""
    del request
    return runtime.authenticator.authenticate(authorization=authorization, api_key_header=x_api_key)


@router.get("/v1/models")
async def list_models(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    identity: Annotated[Identity, Depends(resolve_identity)],
) -> dict[str, Any]:
    """One entry: the cell itself.

    The real model aliases are deliberately not exposed. A client choosing a backend
    would bypass classification-based routing, which is the one decision no caller gets
    to make.

    Authenticated, like every other endpoint on this channel. It was not, and the reply
    carries the tenant id -- so an unauthenticated caller learned which tenant a cell
    belongs to. Small, but `CHANNELS.md` says every channel authenticates and the Copilot
    Studio spec declares `security: [{bearer: []}]` on this operation, so the code was the
    one thing out of step. Every OpenAI-compatible client sends the key on this probe.
    """
    del identity
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_ID,
                "object": "model",
                "created": int(time.time()),
                "owned_by": runtime.profile.identity.tenant_id,
            }
        ],
    }


@router.post("/v1/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    identity: Annotated[Identity, Depends(resolve_identity)],
) -> Any:
    """Run one turn through the graph, streaming or not."""
    state = _build_state(body, identity, runtime)
    bind_request_context(
        trace_id=state.trace_id,
        tenant_id=state.identity.tenant_id,
        task_id=state.task_id,
        thread_id=state.thread_id,
    )

    if body.stream:
        return StreamingResponse(
            _stream(runtime, state),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache",
                "connection": "keep-alive",
                # Streaming through nginx without this buffers the whole response and
                # the "streaming" is a lie the user only discovers in production.
                "x-accel-buffering": "no",
            },
        )

    final = await _invoke(runtime, state)
    return JSONResponse(_completion_body(final))


# --------------------------------------------------------------------- internals


def _build_state(body: ChatCompletionRequest, identity: Identity, runtime: Runtime) -> AgentState:
    thread_id = body.x_thread_id or str(uuid.uuid4())
    return AgentState(
        task_id=body.x_task_id or str(uuid.uuid4()),
        thread_id=thread_id,
        trace_id=str(uuid.uuid4()),
        identity=identity,
        agent_name=runtime.profile.identity.agent_name,
        area=runtime.profile.identity.area,
        messages=[
            Message(role=m.role, content=m.content, name=m.name)
            for m in body.messages
            # A caller-supplied system message would be an instruction-injection channel:
            # the cell's own system prompt is the only one that reaches the model.
            if m.role != "system"
        ],
    )


def _thread_config(runtime: Runtime, state: AgentState) -> dict[str, Any]:
    from agent_forge.core.checkpointer import namespaced_thread_id

    return {
        "configurable": {
            "thread_id": namespaced_thread_id(
                state.identity.tenant_id, runtime.settings.instance, state.thread_id
            )
        }
    }


async def _invoke(runtime: Runtime, state: AgentState) -> AgentState:
    raw = await runtime.graph.ainvoke(state, _thread_config(runtime, state))
    final = _as_state(raw, state)
    if isinstance(raw, dict) and raw.get("__interrupt__"):
        await _publish_approvals(runtime, final, raw["__interrupt__"])
        return final.model_copy(update={"status": "awaiting_approval"})
    return final


async def _publish_approvals(runtime: Runtime, state: AgentState, interrupts: Any) -> None:
    """Put a paused action on the queue an operator can actually see.

    The graph suspends itself through the checkpointer; this makes the pause *visible* at
    /admin/approvals. Without it the task would wait forever for a decision nobody knew
    was needed.
    """
    for item in interrupts:
        payload = getattr(item, "value", None)
        if not isinstance(payload, dict) or payload.get("type") != "approval_required":
            continue
        request = ApprovalRequest(
            id=str(payload.get("request_id") or uuid.uuid4()),
            action_category=payload.get("action_category"),
            description=str(payload.get("description", "")),
            autonomy_required=AutonomyLevel.parse(payload.get("autonomy_required", "A2")),
            required_approvals=int(payload.get("required_approvals", 1)),
            requires_elevated_role=bool(payload.get("requires_elevated_role", False)),
        )
        await runtime.approvals.enqueue(
            PendingApproval(
                request=request,
                task_id=state.task_id,
                thread_id=state.thread_id,
                tenant_id=state.identity.tenant_id,
                agent_name=state.agent_name,
                area=state.area,
            )
        )
        log.info(
            "chat.awaiting_approval",
            request_id=request.id,
            autonomy_required=str(request.autonomy_required),
        )


def _as_state(raw: Any, fallback: AgentState) -> AgentState:
    if isinstance(raw, AgentState):
        return raw
    if isinstance(raw, dict):
        payload = {k: v for k, v in raw.items() if not k.startswith("__")}
        return AgentState.model_validate(payload)
    return fallback


async def _stream(runtime: Runtime, state: AgentState) -> AsyncIterator[str]:
    """Emit ``chat.completion.chunk`` events.

    The graph is invoked once and its answer streamed in pieces rather than token by
    token from the model, because the answer only exists after the quality gate has
    accepted it. Streaming tokens the judge might reject would mean showing the user
    text the cell then has to retract.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    yield _chunk(completion_id, created, role="assistant")
    try:
        final = await _invoke(runtime, state)
    except Exception as exc:
        log.exception("chat.stream_failed")
        yield _chunk(completion_id, created, content=f"\n\n[error: {type(exc).__name__}]")
        yield _chunk(completion_id, created, finish_reason="stop")
        yield "data: [DONE]\n\n"
        return

    for piece in _split_for_stream(final.answer):
        yield _chunk(completion_id, created, content=piece)

    if final.citations:
        yield _chunk(
            completion_id,
            created,
            extra={"x_citations": [c.reference for c in final.citations]},
        )
    if final.status == "awaiting_approval":
        yield _chunk(
            completion_id,
            created,
            extra={
                "x_status": "awaiting_approval",
                "x_task_id": final.task_id,
                "x_thread_id": final.thread_id,
            },
        )
    yield _chunk(completion_id, created, finish_reason="stop")
    yield "data: [DONE]\n\n"


def _split_for_stream(text: str, size: int = 24) -> Sequence[str]:
    """Chunk the answer so clients render progressively rather than in one jump."""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _chunk(
    completion_id: str,
    created: int,
    *,
    role: str | None = None,
    content: str | None = None,
    finish_reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    delta: dict[str, Any] = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    payload: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if extra:
        payload.update(extra)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _completion_body(state: AgentState) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{state.task_id[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": state.answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": state.usage.tokens_in,
            "completion_tokens": state.usage.tokens_out,
            "total_tokens": state.usage.tokens_in + state.usage.tokens_out,
        },
        "x_thread_id": state.thread_id,
        "x_task_id": state.task_id,
        "x_status": state.status,
        "x_classification": str(state.classification),
        "x_citations": [c.reference for c in state.citations],
    }
