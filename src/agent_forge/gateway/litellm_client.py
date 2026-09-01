"""Model gateway: the only way the cell talks to a language model.

Two layers, deliberately separated:

* ``LiteLLMTransport`` -- moves bytes to the LiteLLM proxy. Knows nothing about
  classification.
* ``GovernedGateway`` -- wraps any transport and calls ``ModelPolicy.assert_allowed``
  immediately before the request leaves the process. Nothing in the codebase talks to a
  transport directly, so the sovereignty check cannot be forgotten by a future caller.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from agent_forge.core.classification import Classification
from agent_forge.core.errors import ModelGatewayError
from agent_forge.gateway.model_policy import ModelPolicy, RoutingDecision
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT = 120.0


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """One model call, already routed."""

    messages: Sequence[Mapping[str, Any]]
    model: str
    temperature: float = 0.2
    max_tokens: int | None = None
    tools: Sequence[Mapping[str, Any]] | None = None
    tenant_id: str = ""
    trace_id: str = ""
    stop: Sequence[str] | None = None


@dataclass(frozen=True, slots=True)
class ChatChunk:
    """One streamed fragment."""

    delta: str = ""
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ChatResponse:
    """A completed model call plus its accounting."""

    content: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    finish_reason: str | None = None
    tool_calls: tuple[Mapping[str, Any], ...] = field(default=())


@runtime_checkable
class ModelTransport(Protocol):
    """Anything that can carry a chat request to a backend."""

    async def complete(self, request: ChatRequest) -> ChatResponse: ...

    def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]: ...

    async def health(self) -> bool: ...

    async def aclose(self) -> None: ...


class LiteLLMTransport:
    """Talks to the LiteLLM proxy over its OpenAI-compatible HTTP API.

    Going through the proxy rather than importing the ``litellm`` SDK in-process is what
    gives us per-tenant virtual keys, budgets and cost logging for free, and keeps the
    ``agent-api`` image free of provider SDKs.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"authorization": f"Bearer {api_key}"},
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LiteLLMTransport:
        source = env if env is not None else os.environ
        base_url = source.get("LITELLM_BASE_URL", "http://litellm:4000")
        api_key = source.get("LITELLM_MASTER_KEY", "")
        if not api_key:
            raise ModelGatewayError(
                "LITELLM_MASTER_KEY is not set; the model gateway would be unauthenticated"
            )
        return cls(base_url, api_key)

    def _payload(self, request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": list(request.messages),
            "temperature": request.temperature,
            "stream": stream,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.tools:
            payload["tools"] = list(request.tools)
        if request.stop:
            payload["stop"] = list(request.stop)
        # LiteLLM attributes spend to the virtual key and to these tags.
        payload["metadata"] = {
            "tenant_id": request.tenant_id,
            "trace_id": request.trace_id,
        }
        return payload

    async def complete(self, request: ChatRequest) -> ChatResponse:
        try:
            response = await self._client.post(
                "/v1/chat/completions", json=self._payload(request, stream=False)
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise ModelGatewayError(
                "model gateway rejected the request",
                model=request.model,
                status=exc.response.status_code,
                detail=exc.response.text[:500],
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelGatewayError(
                "model gateway unreachable", model=request.model, detail=str(exc)
            ) from exc

        return _parse_completion(body, request.model)

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        """Yield fragments as the backend produces them (SSE end to end)."""
        try:
            async with self._client.stream(
                "POST", "/v1/chat/completions", json=self._payload(request, stream=True)
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    chunk = _parse_sse_line(line)
                    if chunk is not None:
                        yield chunk
        except httpx.HTTPStatusError as exc:
            raise ModelGatewayError(
                "model gateway rejected the stream",
                model=request.model,
                status=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelGatewayError(
                "model gateway stream failed", model=request.model, detail=str(exc)
            ) from exc

    async def health(self) -> bool:
        try:
            response = await self._client.get("/health/liveliness", timeout=5.0)
        except httpx.HTTPError:
            return False
        return response.status_code < 400

    async def aclose(self) -> None:
        await self._client.aclose()


class GovernedGateway:
    """A transport plus the sovereignty guarantee.

    Every call is routed by ``ModelPolicy`` from the task's cumulative classification and
    re-checked immediately before it leaves. Callers pass a classification, never a model
    name, so there is no way to address a backend directly.
    """

    def __init__(
        self,
        transport: ModelTransport,
        policy: ModelPolicy,
        *,
        external_allowed: Sequence[str] = (),
    ) -> None:
        self._transport = transport
        self._policy = policy
        self._external_allowed = tuple(external_allowed)

    @property
    def policy(self) -> ModelPolicy:
        return self._policy

    def route(
        self,
        classification: Classification,
        preferred: Sequence[str],
        *,
        purpose: str = "chat",
        require_tools: bool = False,
    ) -> RoutingDecision:
        decision = self._policy.route(
            classification,
            preferred=preferred,
            external_allowed=self._external_allowed,
            purpose=purpose,
            require_tools=require_tools,
        )
        log.debug(
            "model.routed",
            alias=decision.alias,
            classification=str(classification),
            sovereignty=str(decision.backend.sovereignty),
            reason=decision.reason,
        )
        return decision

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        classification: Classification,
        preferred: Sequence[str],
        tenant_id: str = "",
        trace_id: str = "",
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> ChatResponse:
        decision = self.route(classification, preferred, require_tools=bool(tools))
        # Re-check at the wire: state can change between routing and sending.
        self._policy.assert_allowed(decision.alias, classification)
        return await self._transport.complete(
            ChatRequest(
                messages=messages,
                model=decision.alias,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                tenant_id=tenant_id,
                trace_id=trace_id,
            )
        )

    async def stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        classification: Classification,
        preferred: Sequence[str],
        tenant_id: str = "",
        trace_id: str = "",
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ChatChunk]:
        decision = self.route(classification, preferred)
        self._policy.assert_allowed(decision.alias, classification)
        async for chunk in self._transport.stream(
            ChatRequest(
                messages=messages,
                model=decision.alias,
                temperature=temperature,
                max_tokens=max_tokens,
                tenant_id=tenant_id,
                trace_id=trace_id,
            )
        ):
            yield chunk

    async def health(self) -> bool:
        return await self._transport.health()

    async def aclose(self) -> None:
        await self._transport.aclose()


# ------------------------------------------------------------------- parsing


def _parse_completion(body: Mapping[str, Any], fallback_model: str) -> ChatResponse:
    choices = body.get("choices") or []
    if not choices:
        raise ModelGatewayError("model returned no choices", model=fallback_model)
    choice = choices[0]
    message = choice.get("message") or {}
    usage = body.get("usage") or {}
    raw_tools = message.get("tool_calls") or []
    return ChatResponse(
        content=message.get("content") or "",
        model=str(body.get("model") or fallback_model),
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        # LiteLLM reports spend in a response header on the proxy; when the body carries
        # it we use it, otherwise cost is attributed by the proxy's own spend log.
        cost_usd=float(body.get("response_cost") or 0.0),
        finish_reason=choice.get("finish_reason"),
        tool_calls=tuple(raw_tools),
    )


def _parse_sse_line(line: str) -> ChatChunk | None:
    """Turn one SSE line into a chunk, or None for keep-alives and terminators."""
    if not line or not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    import json

    try:
        body = json.loads(payload)
    except json.JSONDecodeError:
        return None
    choices = body.get("choices") or []
    if not choices:
        return None
    choice = choices[0]
    delta = choice.get("delta") or {}
    return ChatChunk(
        delta=delta.get("content") or "",
        finish_reason=choice.get("finish_reason"),
    )
