"""The upstream surfaces: task lifecycle, MCP server, A2A and the Copilot Studio spec.

The phase's exit criteria are the first three tests in the MCP and A2A sections: a real
MCP client lists the tools and invokes ``run_task``, and the Agent Card validates against
the fields A2A requires. They drive the mounted app over ASGI rather than a socket, so
what is exercised is the same wiring a deployed cell exposes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from agent_forge.core.classification import Classification
from agent_forge.core.state import AgentState, Citation, Identity, Message
from agent_forge.upstream.mcp_server import TOOL_NAMES, identity_from_arguments
from agent_forge.upstream.openapi import OPERATION_PATHS, sanitise_for_copilot_studio, simplify
from agent_forge.upstream.tasks import InMemoryTaskStore, TaskRecord, TaskRunner, TaskState
from tests.cell import AUTH, PUBLIC_URL, TENANT, make_app, make_runtime
from tests.support import FakeTransport


def _identity(**kwargs: Any) -> Identity:
    base: dict[str, Any] = {
        "tenant_id": TENANT,
        "user_id": "u1",
        "groups": ("finanzas",),
        "classification_ceiling": Classification.C2,
        "authenticated": True,
    }
    base.update(kwargs)
    return Identity(**base)


# ------------------------------------------------------------------ task lifecycle


def _finished(status: str = "completed", answer: str = "listo") -> AgentState:
    return AgentState(
        identity=_identity(),
        status=status,
        answer=answer,
        citations=[Citation(source_id="policy.pdf", chunk_id="p1")],
        classification=Classification.C2,
        messages=[Message(role="user", content="q")],
    )


async def test_ask_runs_to_completion_and_records_the_answer() -> None:
    runner = TaskRunner(lambda state: _echo(state, _finished()))

    record = await runner.ask("cual es el limite?", _identity())

    assert record.state is TaskState.COMPLETED
    assert record.answer == "listo"
    assert record.citations == ("policy.pdf#p1",)


async def test_a_paused_graph_surfaces_upstream_as_input_required() -> None:
    """A HITL pause must be visible, not look like a task that quietly hangs."""
    runner = TaskRunner(lambda state: _echo(state, _finished(status="awaiting_approval")))

    record = await runner.ask("borra la factura", _identity())

    assert record.state is TaskState.INPUT_REQUIRED
    assert not record.state.is_terminal


async def test_a_failing_graph_becomes_a_failed_task_not_an_exception() -> None:
    async def explode(state: AgentState) -> AgentState:
        raise RuntimeError("el modelo no respondio")

    runner = TaskRunner(explode)

    record = await runner.ask("q", _identity())

    assert record.state is TaskState.FAILED
    assert "el modelo no respondio" in record.error


async def test_submit_returns_before_the_answer_and_status_finds_it_later() -> None:
    runner = TaskRunner(lambda state: _echo(state, _finished()))

    record = await runner.submit("q", _identity())
    assert record.state is TaskState.SUBMITTED

    await runner.wait(record.task_id, timeout=5.0)
    found = await runner.status(TENANT, record.task_id)
    assert found.state is TaskState.COMPLETED


async def test_a_task_belongs_to_its_tenant() -> None:
    """Looking a task up under another tenant must not find it."""
    from agent_forge.core.errors import TaskNotFoundError

    runner = TaskRunner(lambda state: _echo(state, _finished()))
    record = await runner.ask("q", _identity())

    with pytest.raises(TaskNotFoundError):
        await runner.status("otro-tenant", record.task_id)


async def test_cancelling_a_finished_task_reports_it_rather_than_rewriting_it() -> None:
    runner = TaskRunner(lambda state: _echo(state, _finished()))
    record = await runner.ask("q", _identity())

    cancelled = await runner.cancel(TENANT, record.task_id)

    assert cancelled.state is TaskState.COMPLETED


async def test_the_store_evicts_oldest_records_first() -> None:
    store = InMemoryTaskStore(max_records=2)
    for index in range(3):
        await store.put(TaskRecord(task_id=f"t{index}", thread_id="th", tenant_id=TENANT))

    remaining = {r.task_id for r in await store.list_for(TENANT)}
    assert remaining == {"t1", "t2"}


def test_the_a2a_representation_carries_citations_as_their_own_part() -> None:
    record = TaskRecord(task_id="t1", thread_id="th", tenant_id=TENANT, answer="ok")
    record.citations = ("policy.pdf#p1",)
    record.state = TaskState.COMPLETED

    payload = record.to_a2a()

    parts = payload["artifacts"][0]["parts"]
    assert [p["kind"] for p in parts] == ["text", "data"]
    assert parts[1]["data"]["citations"] == ["policy.pdf#p1"]


async def _echo(state: AgentState, result: AgentState) -> AgentState:
    del state
    return result


# ------------------------------------------------------------------ MCP identity


def test_an_orchestrator_that_propagates_nobody_gets_an_anonymous_c0_requester() -> None:
    """A supervising agent is a caller, not an administrator."""
    identity = identity_from_arguments(TENANT, None)

    assert identity.is_anonymous
    assert identity.classification_ceiling == Classification.C0


def test_a_propagated_user_cannot_claim_a_ceiling_above_the_cells_own() -> None:
    identity = identity_from_arguments(
        TENANT,
        {"user_id": "u1", "groups": ["finanzas"], "classification_ceiling": "C4"},
        max_ceiling=Classification.C2,
    )

    assert identity.classification_ceiling == Classification.C2
    assert identity.groups == ("finanzas",)


def test_an_unparseable_ceiling_falls_to_c0_rather_than_being_ignored() -> None:
    identity = identity_from_arguments(
        TENANT, {"user_id": "u1", "classification_ceiling": "banana"}
    )

    assert identity.classification_ceiling == Classification.C0


# ------------------------------------------------------------------ MCP over HTTP


@pytest.fixture
def app() -> FastAPI:
    runtime = make_runtime(FakeTransport(replies=["El limite es 1500 MXN."]))
    return make_app(runtime)


@pytest.fixture
def mcp_app() -> FastAPI:
    """An app with the MCP surface on, built per test rather than per fixture chain."""
    runtime = make_runtime(
        FakeTransport(replies=["El limite es 1500 MXN."]), enable=("mcp_server",)
    )
    return make_app(runtime)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def test_an_external_mcp_client_lists_the_tools_and_invokes_run_task(
    mcp_app: FastAPI,
) -> None:
    """The phase's exit criterion, exercised with the real MCP client library."""
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with mcp_app.router.lifespan_context(mcp_app):
        # `follow_redirects` is what the MCP SDK's own client always sets, and it is
        # what carries `/mcp` to the canonical `/mcp/`.
        http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=mcp_app),
            base_url="http://test",
            follow_redirects=True,
        )
        async with (
            http,
            streamable_http_client("http://test/mcp", http_client=http) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()

            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == {name.replace(".", "_") for name in TOOL_NAMES}

            result = await session.call_tool(
                "run_task",
                {"question": "cual es el limite?", "requester": {"user_id": "u1"}},
            )
            assert not result.is_error
            payload = json.loads(result.content[0].text)
            assert payload["task_id"]
            assert payload["state"] in {"submitted", "working"}

            status = await session.call_tool("get_status", {"task_id": payload["task_id"]})
            assert json.loads(status.content[0].text)["task_id"] == payload["task_id"]


# ------------------------------------------------------------------------- A2A


async def test_the_agent_card_is_served_unauthenticated_and_advertises_skills(
    client: httpx.AsyncClient,
) -> None:
    """Discovery precedes authentication, so the card must answer without a credential."""
    response = await client.get("/.well-known/agent.json")

    assert response.status_code == 200
    card = response.json()
    assert card["protocolVersion"]
    assert card["url"] == f"{PUBLIC_URL}/a2a"
    assert card["preferredTransport"] == "JSONRPC"
    assert {"name", "description", "version", "skills", "capabilities"} <= set(card)
    assert all({"id", "name", "description", "tags"} <= set(s) for s in card["skills"])


async def test_the_agent_card_does_not_disclose_the_tenants_tool_catalogue(
    client: httpx.AsyncClient,
) -> None:
    """The card is public; naming internal systems in it is disclosure for no benefit."""
    card = (await client.get("/.well-known/agent.json")).json()

    assert "tools" not in card
    serialised = json.dumps(card)
    assert "jira" not in serialised.lower()
    assert "mcp_gateway" not in serialised.lower()


async def test_both_spellings_of_the_well_known_path_serve_the_same_card(
    client: httpx.AsyncClient,
) -> None:
    first = await client.get("/.well-known/agent.json")
    second = await client.get("/.well-known/agent-card.json")

    assert first.json() == second.json()


async def test_message_send_returns_a_task_with_an_artifact(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/a2a",
        headers=AUTH,
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "cual es el limite?"}],
                }
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    task = body["result"]
    assert task["kind"] == "task"
    assert task["status"]["state"] == "completed"
    assert task["artifacts"][0]["parts"][0]["text"]


async def test_tasks_get_finds_the_task_message_send_created(client: httpx.AsyncClient) -> None:
    created = (
        await client.post(
            "/a2a",
            headers=AUTH,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {"message": {"parts": [{"kind": "text", "text": "hola"}]}},
            },
        )
    ).json()["result"]

    found = await client.post(
        "/a2a",
        headers=AUTH,
        json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": created["id"]}},
    )

    assert found.json()["result"]["id"] == created["id"]


async def test_an_unsupported_method_is_a_json_rpc_error_not_a_crash(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/a2a",
        headers=AUTH,
        json={"jsonrpc": "2.0", "id": 9, "method": "tasks/resubscribe", "params": {}},
    )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32602


async def test_a2a_requires_a_credential(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {"message": {"parts": [{"kind": "text", "text": "hola"}]}},
        },
    )

    assert response.status_code == 401


# --------------------------------------------------------------- OpenAPI for Copilot


async def test_the_copilot_spec_only_exposes_the_operations_an_orchestrator_should_call(
    client: httpx.AsyncClient,
) -> None:
    spec = (await client.get("/openapi/copilot-studio.json")).json()

    assert spec["openapi"] == "3.1.0"
    assert set(spec["paths"]) <= OPERATION_PATHS
    assert "/admin/approvals" not in spec["paths"]
    assert "/healthz" not in spec["paths"]
    assert spec["servers"][0]["url"] == PUBLIC_URL


async def test_the_copilot_spec_carries_no_construct_the_platform_rejects(
    client: httpx.AsyncClient,
) -> None:
    """Unresolved $ref, nullable anyOf and open objects are what Copilot Studio refuses."""
    spec = (await client.get("/openapi/copilot-studio.json")).json()

    serialised = json.dumps(spec)
    assert "$ref" not in serialised
    assert "anyOf" not in serialised
    for operations in spec["paths"].values():
        for operation in operations.values():
            assert operation["operationId"]
            assert "200" in operation["responses"]


def test_simplify_inlines_a_reference_and_closes_the_object() -> None:
    components = {"Ask": {"type": "object", "properties": {"q": {"type": "string"}}}}

    result = simplify({"$ref": "#/components/schemas/Ask"}, components)

    assert result["properties"]["q"]["type"] == "string"
    assert result["additionalProperties"] is False


def test_simplify_collapses_the_nullable_union_fastapi_emits() -> None:
    result = simplify({"anyOf": [{"type": "string"}, {"type": "null"}]}, {})

    assert result == {"type": "string"}


def test_simplify_stops_at_a_reference_cycle_instead_of_recursing_forever() -> None:
    components = {"Node": {"type": "object", "properties": {"next": {"$ref": "#/c/Node"}}}}

    result = simplify({"$ref": "#/c/Node"}, components)

    assert result["type"] == "object"


def test_a_missing_reference_degrades_to_an_open_type_rather_than_raising() -> None:
    assert simplify({"$ref": "#/components/schemas/Gone"}, {}) == {"type": "object"}


def test_the_sanitiser_drops_paths_it_was_not_asked_for() -> None:
    spec = {
        "info": {"version": "1.2.3"},
        "paths": {
            "/v1/models": {"get": {"operationId": "models"}},
            "/admin/secret": {"get": {"operationId": "secret"}},
        },
    }

    result = sanitise_for_copilot_studio(spec, paths={"/v1/models"})

    assert list(result["paths"]) == ["/v1/models"]
    assert result["info"]["version"] == "1.2.3"
