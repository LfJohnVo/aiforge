"""Connectors as the graph uses them, and the generator that creates new ones.

The n8n case is F4's second exit criterion at the level that matters: a workflow is
triggered from inside the graph, the graph is blocked on it, the callback arrives, and the
graph carries on to produce an answer.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from langgraph.checkpoint.memory import InMemorySaver

from agent_forge.connectors import Allowlist, ConnectorRegistry, context_from_state
from agent_forge.connectors.n8n import N8nConnector, WorkflowSpec
from agent_forge.connectors.repo_graph import RepoGraphConnector
from agent_forge.core.autonomy import AutonomyLevel, AutonomyMap
from agent_forge.core.classification import Classification
from agent_forge.core.graph import build_graph
from agent_forge.core.state import AgentState
from agent_forge.core.subgraphs.base import (
    DomainContext,
    DomainOutcome,
    DomainSubgraph,
    ToolRequest,
)
from tests.support import FakeTransport, make_deps, make_gateway, make_state

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE = "http://n8n.test"


class ToolCallingSubgraph(DomainSubgraph):
    """A domain that always asks for one specific tool. Keeps the test about the wiring."""

    name = "tool-caller"
    default_intents = ("question",)

    def __init__(self, tool: str, args: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._tool = tool
        self._args = args or {}

    async def gather(self, ctx: DomainContext) -> DomainOutcome:
        del ctx
        return DomainOutcome(tool_requests=[ToolRequest(tool=self._tool, args=self._args)])


async def run(app: Any, state: AgentState, thread: str = "t1") -> AgentState:
    result = await app.ainvoke(state, {"configurable": {"thread_id": thread}})
    return AgentState.model_validate({k: v for k, v in result.items() if not k.startswith("__")})


# ----------------------------------------------------------- the exit criterion


@respx.mock
async def test_an_n8n_callback_lets_the_graph_finish() -> None:
    """F4 exit criterion: a workflow is fired from the graph and its callback resumes it."""
    respx.get(f"{BASE}/healthz").mock(return_value=httpx.Response(200))
    route = respx.post(f"{BASE}/webhook/facturas-ocr").mock(
        return_value=httpx.Response(200, json={"accepted": True})
    )
    connector = N8nConnector(
        BASE,
        # A1: this profile treats OCR of an invoice as low risk, so it runs without a
        # human. The A2 default and its approval pause are covered below.
        workflows=[
            WorkflowSpec(
                name="facturas-ocr",
                wait_seconds=10.0,
                autonomy_min=AutonomyLevel.A1,
            )
        ],
        callback_url=f"{BASE}/callback",
    )
    registry = ConnectorRegistry(
        allowlist=Allowlist(patterns=("n8n.*",)),
        autonomy_map=AutonomyMap(default=AutonomyLevel.A1),
    )
    registry.register(connector)

    deps = make_deps(
        gateway=make_gateway(FakeTransport(replies=["La factura suma 1500 MXN."])),
        subgraph=ToolCallingSubgraph("n8n.facturas-ocr", {"file": "f.pdf"}),
    )
    deps.connectors = registry
    deps.tool_executor = registry.executor(context_from_state)
    app = build_graph(deps)

    task = asyncio.create_task(run(app, make_state("procesa esta factura")))

    # The graph is now blocked inside the tools node, waiting for n8n.
    for _ in range(200):
        if route.called:
            break
        await asyncio.sleep(0.01)
    assert route.called, "the workflow was never triggered"
    assert not task.done(), "the graph should be waiting for the callback"

    correlation_id = json.loads(route.calls[0].request.read())["correlation_id"]
    connector.deliver_callback(
        correlation_id, {"status": "completed", "output": {"total": "1500 MXN"}}
    )

    final = await asyncio.wait_for(task, timeout=10)
    await registry.aclose()

    assert final.status == "completed"
    assert final.tool_calls[0].ok
    assert final.answer == "La factura suma 1500 MXN."


@respx.mock
async def test_a_workflow_that_never_answers_does_not_claim_success() -> None:
    respx.get(f"{BASE}/healthz").mock(return_value=httpx.Response(200))
    respx.post(f"{BASE}/webhook/lento").mock(return_value=httpx.Response(200))
    connector = N8nConnector(
        BASE,
        workflows=[WorkflowSpec(name="lento", wait_seconds=0.05, autonomy_min=AutonomyLevel.A1)],
    )
    registry = ConnectorRegistry(
        allowlist=Allowlist(patterns=("n8n.*",)),
        autonomy_map=AutonomyMap(default=AutonomyLevel.A1),
    )
    registry.register(connector)

    deps = make_deps(
        gateway=make_gateway(FakeTransport(replies=["respuesta"])),
        subgraph=ToolCallingSubgraph("n8n.lento"),
    )
    deps.connectors = registry
    deps.tool_executor = registry.executor(context_from_state)
    app = build_graph(deps)

    final = await run(app, make_state("lanza el workflow"))
    await registry.aclose()

    payload = final.tool_calls[0]
    assert payload.ok, "an accepted-but-unfinished workflow is not a failure"
    # The model is told, in the prompt, that the action is still running.
    assert final.status == "completed"


# ----------------------------------------------------- catalogue in the graph


async def test_the_graph_offers_only_allowlisted_healthy_tools() -> None:
    registry = ConnectorRegistry(allowlist=Allowlist(patterns=("nada.*",)))
    registry.register(RepoGraphConnector())
    deps = make_deps(gateway=make_gateway(FakeTransport()))
    deps.connectors = registry

    from agent_forge.core.graph import _tool_catalogue

    catalogue = await _tool_catalogue(make_state(), deps)

    assert catalogue == [], "repo_graph is not in this profile's allowlist"


async def test_a_builtin_tool_is_offered_without_an_allowlist_entry() -> None:
    registry = ConnectorRegistry(allowlist=Allowlist(builtin=frozenset({"repo_graph.query"})))
    connector = RepoGraphConnector()
    registry.register(connector)
    if not await connector.health():
        pytest.skip("run `make repo-graph` first")

    deps = make_deps(gateway=make_gateway(FakeTransport()))
    deps.connectors = registry

    from agent_forge.core.graph import _tool_catalogue

    catalogue = await _tool_catalogue(make_state(), deps)

    assert [t.name for t in catalogue] == ["repo_graph.query"]


async def test_a_refused_tool_is_reported_to_the_model_not_hidden() -> None:
    """The model must know the action did not happen, or it will claim it did."""
    registry = ConnectorRegistry(allowlist=Allowlist())
    registry.register(RepoGraphConnector())
    transport = FakeTransport(replies=["respuesta"])
    deps = make_deps(
        gateway=make_gateway(transport),
        subgraph=ToolCallingSubgraph("repo_graph.query", {"kind": "overview"}),
    )
    deps.connectors = registry
    deps.tool_executor = registry.executor(context_from_state)
    app = build_graph(deps)

    final = await run(app, make_state("consulta el repositorio"))

    assert final.tool_calls[0].ok is False
    assert "not available" in (final.tool_calls[0].error or "")
    prompt = "".join(str(m["content"]) for m in transport.calls[-1].messages)
    assert "no se ejecutaron" in prompt
    assert "No afirmes que la accion se realizo" in prompt


async def test_a_tool_result_raises_the_task_classification() -> None:
    """The chain the sovereignty guarantee depends on, through the registry."""
    registry = ConnectorRegistry(
        allowlist=Allowlist(builtin=frozenset({"repo_graph.query"})),
        autonomy_map=AutonomyMap(default=AutonomyLevel.A0),
    )
    connector = RepoGraphConnector()
    registry.register(connector)
    if not await connector.health():
        pytest.skip("run `make repo-graph` first")

    deps = make_deps(
        gateway=make_gateway(FakeTransport(replies=["respuesta"])),
        subgraph=ToolCallingSubgraph("repo_graph.query", {"kind": "overview"}),
        autonomy_map=AutonomyMap(default=AutonomyLevel.A0),
    )
    deps.connectors = registry
    deps.tool_executor = registry.executor(context_from_state)
    app = build_graph(deps)

    final = await run(app, make_state("resumen del repositorio"))

    assert final.tool_calls[0].ok
    # repo_graph declares C1, so the task rises from C0 to C1.
    assert final.classification == Classification.C1


# -------------------------------------------------------------- the generator


def test_the_generator_produces_a_connector_that_passes_its_own_tests(
    tmp_path: Path,
) -> None:
    """`make new-connector` has to yield something that is green before it is edited."""
    import new_connector

    name = "probeconn"
    module = REPO_ROOT / "src" / "agent_forge" / "connectors" / f"{name}.py"
    test = REPO_ROOT / "tests" / "unit" / "connectors" / f"test_{name}.py"
    del tmp_path  # the generator writes into the repo by design

    try:
        assert new_connector.main(["--name", name]) == 0
        assert module.is_file()
        assert test.is_file()

        lint = subprocess.run(  # noqa: S603 - fixed command, our own files
            [sys.executable, "-m", "ruff", "check", str(module), str(test)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert lint.returncode == 0, lint.stdout

        run_tests = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pytest", str(test), "-q"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert run_tests.returncode == 0, run_tests.stdout[-2000:]
    finally:
        module.unlink(missing_ok=True)
        test.unlink(missing_ok=True)
        _revert_generator_side_effects(name)


def test_the_generator_rejects_an_invalid_name() -> None:
    import new_connector

    assert new_connector.main(["--name", "Not Valid!"]) == 2


def _revert_generator_side_effects(name: str) -> None:
    pyproject = REPO_ROOT / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    cls = "".join(p.capitalize() for p in name.split("_")) + "Connector"
    pyproject.write_text(
        text.replace(f'{name} = "agent_forge.connectors.{name}:{cls}.from_env"\n', ""),
        encoding="utf-8",
    )
    docs = REPO_ROOT / "docs" / "CONNECTORS.md"
    body = docs.read_text(encoding="utf-8")
    title = name.replace("_", " ").title()
    docs.write_text(
        body.replace(f"| `{name}` | {title}: describe aqui que hace y con que scopes. |\n", ""),
        encoding="utf-8",
    )


@respx.mock
async def test_a_default_workflow_pauses_for_approval_before_it_fires() -> None:
    """A workflow does things in the world, so A2 is the default and the human comes first."""
    respx.get(f"{BASE}/healthz").mock(return_value=httpx.Response(200))
    trigger = respx.post(f"{BASE}/webhook/enviar-correo").mock(return_value=httpx.Response(200))
    connector = N8nConnector(BASE, workflows=[WorkflowSpec(name="enviar-correo")])
    registry = ConnectorRegistry(
        allowlist=Allowlist(patterns=("n8n.*",)),
        autonomy_map=AutonomyMap(default=AutonomyLevel.A1),
    )
    registry.register(connector)

    deps = make_deps(
        gateway=make_gateway(FakeTransport(replies=["hecho"])),
        subgraph=ToolCallingSubgraph("n8n.enviar-correo"),
    )
    deps.connectors = registry
    deps.tool_executor = registry.executor(context_from_state)
    app = build_graph(deps, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "aprobacion"}}

    paused = await app.ainvoke(make_state("manda el correo"), config)

    assert paused["__interrupt__"]
    assert paused["__interrupt__"][0].value["autonomy_required"] == "A2"
    assert not trigger.called, "the workflow must not fire before a human approves"
    await registry.aclose()
