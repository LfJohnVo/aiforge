"""The transport that actually puts bytes on the wire to a model backend.

Worth testing carefully for two reasons: it is the last hop before data leaves the
process, and its error handling decides whether a gateway outage degrades or explodes.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from agent_forge.core.classification import Classification
from agent_forge.core.errors import ModelGatewayError, SovereigntyError
from agent_forge.gateway.litellm_client import (
    ChatRequest,
    GovernedGateway,
    LiteLLMTransport,
    _parse_sse_line,
)
from agent_forge.gateway.model_policy import ModelBackend, ModelPolicy, Sovereignty
from tests.support import FakeTransport, make_policy

BASE = "http://litellm:4000"
COMPLETIONS = f"{BASE}/v1/chat/completions"


def transport() -> LiteLLMTransport:
    return LiteLLMTransport(BASE, "sk-test")


def request(**kwargs: object) -> ChatRequest:
    defaults: dict[str, object] = {
        "messages": [{"role": "user", "content": "hola"}],
        "model": "local/fast",
        "tenant_id": "acme-mx",
        "trace_id": "trace-1",
    }
    defaults.update(kwargs)
    return ChatRequest(**defaults)  # type: ignore[arg-type]


# ------------------------------------------------------------------- completion


@respx.mock
async def test_complete_parses_content_and_usage() -> None:
    respx.post(COMPLETIONS).mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "local/fast",
                "choices": [
                    {"message": {"role": "assistant", "content": "hola"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
                "response_cost": 0.0004,
            },
        )
    )
    client = transport()

    response = await client.complete(request())
    await client.aclose()

    assert response.content == "hola"
    assert (response.tokens_in, response.tokens_out) == (11, 3)
    assert response.cost_usd == pytest.approx(0.0004)
    assert response.finish_reason == "stop"


@respx.mock
async def test_tenant_and_trace_are_sent_as_metadata() -> None:
    """LiteLLM attributes spend per tenant; losing the tag loses cost accounting."""
    route = respx.post(COMPLETIONS).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "x"}}], "usage": {}}
        )
    )
    client = transport()

    await client.complete(request())
    await client.aclose()

    body = route.calls[0].request.content.decode()
    assert '"tenant_id": "acme-mx"' in body.replace(
        '"tenant_id":"acme-mx"', '"tenant_id": "acme-mx"'
    )
    assert "trace-1" in body


@respx.mock
async def test_http_error_becomes_a_domain_error_with_status() -> None:
    respx.post(COMPLETIONS).mock(return_value=httpx.Response(429, text="rate limited"))
    client = transport()

    with pytest.raises(ModelGatewayError) as excinfo:
        await client.complete(request())
    await client.aclose()

    assert excinfo.value.context["status"] == 429
    assert excinfo.value.code == "model_gateway_error"


@respx.mock
async def test_connection_failure_becomes_a_domain_error() -> None:
    respx.post(COMPLETIONS).mock(side_effect=httpx.ConnectError("refused"))
    client = transport()

    with pytest.raises(ModelGatewayError, match="unreachable"):
        await client.complete(request())
    await client.aclose()


@respx.mock
async def test_response_without_choices_is_rejected() -> None:
    respx.post(COMPLETIONS).mock(return_value=httpx.Response(200, json={"choices": []}))
    client = transport()

    with pytest.raises(ModelGatewayError, match="no choices"):
        await client.complete(request())
    await client.aclose()


@respx.mock
async def test_tools_and_stop_are_forwarded_when_present() -> None:
    route = respx.post(COMPLETIONS).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "x"}}], "usage": {}}
        )
    )
    client = transport()

    await client.complete(
        request(
            tools=[{"type": "function", "function": {"name": "f"}}],
            stop=["<END>"],
            max_tokens=42,
        )
    )
    await client.aclose()

    body = route.calls[0].request.content.decode()
    assert '"tools"' in body
    assert "<END>" in body
    assert '"max_tokens"' in body


# ---------------------------------------------------------------------- stream


@respx.mock
async def test_stream_yields_deltas_and_ignores_keepalives() -> None:
    payload = (
        'data: {"choices":[{"delta":{"content":"ho"}}]}\n\n'
        "\n"
        ": keep-alive\n\n"
        'data: {"choices":[{"delta":{"content":"la"},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post(COMPLETIONS).mock(
        return_value=httpx.Response(
            200, text=payload, headers={"content-type": "text/event-stream"}
        )
    )
    client = transport()

    chunks = [chunk async for chunk in client.stream(request())]
    await client.aclose()

    assert "".join(c.delta for c in chunks) == "hola"
    assert chunks[-1].finish_reason == "stop"


@respx.mock
async def test_stream_error_becomes_a_domain_error() -> None:
    respx.post(COMPLETIONS).mock(return_value=httpx.Response(500, text="boom"))
    client = transport()

    with pytest.raises(ModelGatewayError, match="rejected the stream"):
        _ = [chunk async for chunk in client.stream(request())]
    await client.aclose()


@pytest.mark.parametrize(
    "line",
    ["", "event: ping", "data: ", "data: [DONE]", "data: not-json", 'data: {"choices":[]}'],
)
def test_sse_lines_without_a_delta_are_skipped(line: str) -> None:
    assert _parse_sse_line(line) is None


# ---------------------------------------------------------------------- health


@respx.mock
async def test_health_is_true_when_the_proxy_answers() -> None:
    respx.get(f"{BASE}/health/liveliness").mock(return_value=httpx.Response(200))
    client = transport()

    assert await client.health() is True
    await client.aclose()


@respx.mock
async def test_health_is_false_when_the_proxy_is_unreachable() -> None:
    respx.get(f"{BASE}/health/liveliness").mock(side_effect=httpx.ConnectError("nope"))
    client = transport()

    assert await client.health() is False
    await client.aclose()


def test_from_env_refuses_to_run_without_a_key() -> None:
    """An unauthenticated model gateway is a misconfiguration, not a default."""
    with pytest.raises(ModelGatewayError, match="LITELLM_MASTER_KEY"):
        LiteLLMTransport.from_env({"LITELLM_BASE_URL": BASE})


def test_from_env_reads_base_url_and_key() -> None:
    client = LiteLLMTransport.from_env(
        {"LITELLM_BASE_URL": f"{BASE}/", "LITELLM_MASTER_KEY": "sk-x"}
    )

    assert client._base_url == BASE


# ------------------------------------------------------------- governed gateway


async def test_governed_gateway_diverts_classified_traffic_to_a_local_backend() -> None:
    """Asking for an external model with C4 content gets a sovereign one, silently."""
    fake = FakeTransport()
    gateway = GovernedGateway(fake, make_policy(), external_allowed=["anthropic/claude"])

    await gateway.complete(
        [{"role": "user", "content": "secreto"}],
        classification=Classification.C4,
        preferred=["anthropic/claude"],
    )

    assert fake.models_used == ["local/fast"]
    assert gateway.policy.get(fake.models_used[0]).is_local


async def test_governed_gateway_refuses_when_no_sovereign_backend_exists() -> None:
    """With only external backends, C4 must fail closed -- nothing reaches the wire."""
    fake = FakeTransport()
    external_only = ModelPolicy(
        [ModelBackend("anthropic/claude", Sovereignty.EXTERNAL, Classification.C2)]
    )
    gateway = GovernedGateway(fake, external_only, external_allowed=["anthropic/claude"])

    with pytest.raises(SovereigntyError):
        await gateway.complete(
            [{"role": "user", "content": "secreto"}],
            classification=Classification.C4,
            preferred=["anthropic/claude"],
        )

    assert fake.calls == []


async def test_governed_gateway_streams_through_the_transport() -> None:
    fake = FakeTransport(replies=["uno dos tres"])
    gateway = GovernedGateway(fake, make_policy())

    chunks = [
        chunk
        async for chunk in gateway.stream(
            [{"role": "user", "content": "hola"}],
            classification=Classification.C2,
            preferred=["local/fast"],
        )
    ]

    assert "".join(c.delta for c in chunks).strip() == "uno dos tres"
    assert fake.models_used == ["local/fast"]


async def test_governed_gateway_health_and_close_delegate() -> None:
    fake = FakeTransport()
    gateway = GovernedGateway(fake, make_policy())

    assert await gateway.health() is True
    await gateway.aclose()
    assert fake.closed is True
