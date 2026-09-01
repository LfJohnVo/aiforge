"""n8n: trigger a workflow, and have its callback resume the paused graph.

F4's second exit criterion. The interesting part is not the HTTP POST -- it is that a
workflow which finishes *later* resolves the tool call that is still waiting, and that a
workflow which does not finish is reported as unfinished rather than as done.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx

from agent_forge.connectors.base import CallContext
from agent_forge.connectors.n8n import CallbackRegistry, N8nConnector, WorkflowSpec
from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import Classification

BASE = "http://n8n.test"
CTX = CallContext(
    tenant_id="acme-mx",
    user_id="ana",
    groups=("finanzas",),
    classification_ceiling=Classification.C2,
    trace_id="trace-1",
    task_id="task-1",
    autonomy_granted=AutonomyLevel.A1,
)

WORKFLOW = WorkflowSpec(
    name="facturas-ocr",
    description="Procesa una factura con OCR",
    wait_seconds=5.0,
)


def connector(**kwargs: Any) -> N8nConnector:
    return N8nConnector(
        BASE,
        workflows=[WORKFLOW],
        callback_url=f"{BASE}/callback",
        **kwargs,
    )


# ------------------------------------------------------------ the exit criterion


@respx.mock
async def test_a_callback_resolves_the_waiting_tool_call() -> None:
    """F4 exit criterion: the workflow's callback resumes the call that triggered it."""
    route = respx.post(f"{BASE}/webhook/facturas-ocr").mock(
        return_value=httpx.Response(200, json={"accepted": True})
    )
    client = connector()

    call = asyncio.create_task(client.invoke("n8n.facturas-ocr", {"file": "f.pdf"}, CTX))
    # Wait for the trigger to have gone out, then answer as n8n would.
    for _ in range(50):
        if route.called:
            break
        await asyncio.sleep(0.01)
    correlation_id = route.calls[0].request.read().decode()
    correlation_id = _correlation_id(correlation_id)

    delivered = client.deliver_callback(
        correlation_id, {"status": "completed", "output": {"total": "1500 MXN"}}
    )
    result = await call
    await client.aclose()

    assert delivered is True
    assert result.ok
    assert (result.data or {})["status"] == "completed"
    assert (result.data or {})["output"]["output"]["total"] == "1500 MXN"


@respx.mock
async def test_a_workflow_that_does_not_answer_is_reported_as_unfinished() -> None:
    """Claiming a workflow finished when it did not is the lie the graph must not tell."""
    respx.post(f"{BASE}/webhook/lento").mock(return_value=httpx.Response(200))
    client = N8nConnector(BASE, workflows=[WorkflowSpec(name="lento", wait_seconds=0.05)])

    result = await client.invoke("n8n.lento", {}, CTX)
    await client.aclose()

    assert result.ok
    assert (result.data or {})["status"] == "accepted"
    assert "sigue en curso" in (result.data or {})["detail"]


@respx.mock
async def test_the_trigger_carries_correlation_and_identity() -> None:
    route = respx.post(f"{BASE}/webhook/facturas-ocr").mock(return_value=httpx.Response(200))
    client = N8nConnector(
        BASE,
        workflows=[WorkflowSpec(name="facturas-ocr", wait_seconds=0.05)],
        callback_url=f"{BASE}/callback",
    )

    await client.invoke("n8n.facturas-ocr", {"file": "f.pdf"}, CTX)
    await client.aclose()

    body = route.calls[0].request.read().decode()
    assert "correlation_id" in body
    assert "acme-mx" in body
    assert "ana" in body, "the workflow needs the end user, not just the tenant"
    assert "callback" in body


@respx.mock
async def test_a_failed_trigger_is_a_failed_result_and_cancels_the_waiter() -> None:
    respx.post(f"{BASE}/webhook/facturas-ocr").mock(return_value=httpx.Response(500, text="boom"))
    client = connector()

    result = await client.invoke("n8n.facturas-ocr", {}, CTX)
    await client.aclose()

    assert not result.ok
    assert client.callbacks.stats()["pending"] == 0


# ------------------------------------------------------------ callback registry


async def test_a_late_callback_is_kept_rather_than_dropped() -> None:
    """The waiter timing out does not mean the workflow did not run."""
    registry = CallbackRegistry()

    delivered = registry.resolve("nobody-waiting", {"status": "completed"})

    assert delivered is False
    assert registry.stats()["orphans"] == 1
    assert registry.take_orphan("nobody-waiting") == {"status": "completed"}
    assert registry.take_orphan("nobody-waiting") is None


async def test_a_pending_waiter_is_resolved_once() -> None:
    registry = CallbackRegistry()
    waiter = registry.register("c1")

    assert registry.resolve("c1", {"status": "completed"}) is True
    assert await waiter == {"status": "completed"}
    assert registry.resolve("c1", {"status": "again"}) is False


async def test_cancelling_a_waiter_clears_it() -> None:
    registry = CallbackRegistry()
    waiter = registry.register("c1")

    registry.cancel("c1")

    assert waiter.cancelled()
    assert registry.stats()["pending"] == 0


async def test_orphans_expire() -> None:
    registry = CallbackRegistry(orphan_ttl=-1.0)
    registry.resolve("old", {"status": "completed"})

    assert registry.take_orphan("old") is None


# ------------------------------------------------------------------- contract


def test_workflows_default_to_requiring_approval() -> None:
    """A workflow does things in the world."""
    spec = next(s for s in connector().capabilities() if s.name == "n8n.facturas-ocr")

    assert spec.autonomy_min >= AutonomyLevel.A2
    assert not spec.readonly


async def test_an_unknown_workflow_is_rejected() -> None:
    result = await connector().invoke("n8n.no-existe", {}, CTX)

    assert not result.ok


async def test_a_cell_without_n8n_configured_is_unhealthy() -> None:
    assert await N8nConnector("").health() is False


@respx.mock
async def test_health_follows_the_server() -> None:
    respx.get(f"{BASE}/healthz").mock(return_value=httpx.Response(200))
    client = connector()

    assert await client.health() is True
    await client.aclose()


def test_from_env_builds_the_callback_url_from_the_public_address() -> None:
    client = N8nConnector.from_env({"N8N_URL": BASE, "AGENT_PUBLIC_URL": "https://cell.example"})

    assert client.name == "n8n"


def _correlation_id(body: str) -> str:
    import json

    return str(json.loads(body)["correlation_id"])


@pytest.mark.parametrize("path", ["webhook/facturas-ocr", "custom/path"])
def test_the_webhook_path_is_configurable(path: str) -> None:
    workflow = WorkflowSpec(name="x", webhook_path=path if "/" in path else "")

    assert workflow.path.startswith(("webhook/", "custom/"))
