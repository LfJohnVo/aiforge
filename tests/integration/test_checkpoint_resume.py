"""Durable resume against a real Postgres checkpointer.

F1's hardest exit criterion: a task interrupted mid-graph resumes from the exact node it
stopped at, in a *different process object*, with nothing shared but the database. The
unit test in ``tests/unit/test_graph.py`` proves the graph logic; this proves the
persistence, which is where the failure actually costs a customer their work.

Requires Docker. Skipped automatically when it is not available.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from langgraph.types import Command

from agent_forge.core.autonomy import AutonomyLevel, AutonomyMap
from agent_forge.core.checkpointer import namespaced_thread_id, open_checkpointer
from agent_forge.core.graph import build_graph
from agent_forge.core.state import AgentState
from agent_forge.core.subgraphs.base import ToolDescriptor
from agent_forge.core.subgraphs.it_support import ItSupportSubgraph
from tests.support import make_deps, make_state

pytestmark = pytest.mark.integration

TICKET_TOOL = ToolDescriptor(
    name="jira.create_ticket", description="Create a ticket", autonomy_min=AutonomyLevel.A1
)


@pytest.fixture(scope="module")
def postgres_dsn() -> Any:
    testcontainers = pytest.importorskip("testcontainers.postgres")
    try:
        container = testcontainers.PostgresContainer("postgres:18.6-alpine", driver=None)
        container.start()
    except Exception as exc:  # no docker here means skip, not fail
        pytest.skip(f"docker unavailable for integration tests: {exc}")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture
async def saver(postgres_dsn: str) -> AsyncIterator[Any]:
    async with open_checkpointer("postgres", postgres_dsn=postgres_dsn) as checkpointer:
        yield checkpointer


async def test_task_resumes_across_process_objects(saver: Any) -> None:
    executed: list[str] = []

    async def executor(tool: str, args: dict[str, Any], state: AgentState) -> dict[str, Any]:
        del args, state
        executed.append(tool)
        return {"ok": True}

    def fresh_app() -> Any:
        """A brand new graph and dependency object each time: only Postgres persists."""
        return build_graph(
            make_deps(
                subgraph=ItSupportSubgraph(),
                tool_catalog=[TICKET_TOOL],
                tool_executor=executor,
                autonomy_map=AutonomyMap(
                    default=AutonomyLevel.A1,
                    overrides={"gestionar_accesos": AutonomyLevel.A2},
                ),
            ),
            checkpointer=saver,
        )

    thread = namespaced_thread_id("acme-mx", "itest", "resume-1")
    config = {"configurable": {"thread_id": thread}}

    paused = await fresh_app().ainvoke(make_state("necesito acceso al ERP"), config)
    assert paused["__interrupt__"]
    assert executed == []

    # Everything in memory is thrown away; the checkpoint in Postgres is all that is left.
    resumed = await fresh_app().ainvoke(
        Command(resume={"approved": True, "approver": "lider-1"}), config
    )
    final = AgentState.model_validate({k: v for k, v in resumed.items() if not k.startswith("__")})

    assert executed == ["jira.create_ticket"], "the tool must run exactly once, after approval"
    assert final.status == "completed"
    assert final.approvals[0].approvals == ("lider-1",)


async def test_state_survives_and_is_readable_after_the_pause(saver: Any) -> None:
    """An operator must be able to inspect a paused task, not just resume it."""
    deps = make_deps(
        subgraph=ItSupportSubgraph(),
        tool_catalog=[TICKET_TOOL],
        tool_executor=None,
        autonomy_map=AutonomyMap(
            default=AutonomyLevel.A1, overrides={"gestionar_accesos": AutonomyLevel.A3}
        ),
    )
    app = build_graph(deps, checkpointer=saver)
    thread = namespaced_thread_id("acme-mx", "itest", "inspect-1")
    config = {"configurable": {"thread_id": thread}}

    await app.ainvoke(make_state("necesito acceso al ERP"), config)
    snapshot = await app.aget_state(config)

    assert snapshot.next == ("tools",), "must be parked at the node that interrupted"
    state = AgentState.model_validate(snapshot.values)
    assert state.plan[0].action_category == "gestionar_accesos"
    assert state.identity.tenant_id == "acme-mx"


async def test_two_tenants_never_share_a_thread(saver: Any) -> None:
    """Namespacing is part of the key, so the same thread name cannot collide."""
    deps = make_deps()
    app = build_graph(deps, checkpointer=saver)

    for tenant in ("tenant-a", "tenant-b"):
        await app.ainvoke(
            make_state(f"hola desde {tenant}", tenant_id=tenant),
            {"configurable": {"thread_id": namespaced_thread_id(tenant, "itest", "same-name")}},
        )

    for tenant in ("tenant-a", "tenant-b"):
        snapshot = await app.aget_state(
            {"configurable": {"thread_id": namespaced_thread_id(tenant, "itest", "same-name")}}
        )
        state = AgentState.model_validate(snapshot.values)
        assert state.identity.tenant_id == tenant
        assert tenant in state.last_user_message
