"""The agentic graph: flow, classification accumulation, HITL and resume.

Covers F1's exit criteria that do not need live infrastructure.
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent_forge.core.autonomy import AutonomyLevel, AutonomyMap
from agent_forge.core.classification import Classification
from agent_forge.core.graph import GateOutcome, QualityVerdict, build_graph, identity_gate
from agent_forge.core.state import AgentState, Citation
from agent_forge.core.subgraphs.base import ToolDescriptor
from agent_forge.core.subgraphs.it_support import ItSupportSubgraph
from agent_forge.gateway.model_policy import Sovereignty
from tests.support import (
    FakeRetrievalResult,
    FakeTransport,
    fake_retriever,
    make_deps,
    make_gateway,
    make_state,
)

TICKET_TOOL = ToolDescriptor(
    name="jira.create_ticket",
    description="Create a support ticket",
    autonomy_min=AutonomyLevel.A1,
)


async def run(app: Any, state: AgentState, thread: str = "t1") -> AgentState:
    result = await app.ainvoke(state, {"configurable": {"thread_id": thread}})
    return AgentState.model_validate(result)


# --------------------------------------------------------------------- happy path


async def test_end_to_end_produces_an_answer() -> None:
    transport = FakeTransport(replies=["La politica de viaticos permite 1500 MXN."])
    app = build_graph(make_deps(gateway=make_gateway(transport)))

    final = await run(app, make_state("cual es el limite de viaticos"))

    assert final.status == "completed"
    assert final.answer.startswith("La politica")
    assert final.usage.model_calls == 1
    assert final.finished_at is not None


async def test_retrieved_material_becomes_citations() -> None:
    citation = Citation(
        source_id="folder:///data/politica.pdf",
        chunk_id="p3",
        classification=Classification.C2,
    )
    deps = make_deps(retrieve=fake_retriever([FakeRetrievalResult("Limite: 1500 MXN.", citation)]))
    app = build_graph(deps)

    final = await run(app, make_state("limite de viaticos"))

    assert [c.reference for c in final.citations] == ["folder:///data/politica.pdf#p3"]


# ------------------------------------------------------- classification behaviour


async def test_retrieved_classification_raises_the_task_ceiling() -> None:
    """A C3 chunk makes the whole task C3, which forces a sovereign backend."""
    transport = FakeTransport()
    citation = Citation(source_id="s", chunk_id="1", classification=Classification.C3)
    deps = make_deps(
        gateway=make_gateway(transport),
        retrieve=fake_retriever([FakeRetrievalResult("secreto", citation)]),
    )
    app = build_graph(deps)

    final = await run(app, make_state("dime"))

    assert final.classification == Classification.C3
    assert transport.models_used == ["local/quality"]


async def test_tool_result_classification_is_folded_in() -> None:
    async def executor(tool: str, args: dict[str, Any], state: AgentState) -> dict[str, Any]:
        del tool, args, state
        return {"ok": True, "classification": "C4"}

    class RequestsTool(ItSupportSubgraph):
        pass

    deps = make_deps(
        subgraph=RequestsTool(),
        tool_catalog=[TICKET_TOOL],
        tool_executor=executor,
        autonomy_map=AutonomyMap(default=AutonomyLevel.A1),
    )
    app = build_graph(deps)

    final = await run(app, make_state("necesito acceso al ERP"))

    assert final.classification == Classification.C4
    assert final.tool_calls[0].ok


async def test_anonymous_requests_are_capped_at_c0_and_a0() -> None:
    deps = make_deps()
    app = build_graph(deps)

    final = await run(app, make_state("hola", user_id=None, authenticated=False))

    assert final.identity.classification_ceiling == Classification.C0
    assert final.autonomy == AutonomyLevel.A0


async def test_identity_gate_reports_why_it_capped() -> None:
    state = make_state("hola", user_id=None, authenticated=False)

    outcome = await identity_gate(state)

    assert outcome.classification_ceiling == Classification.C0
    assert outcome.autonomy_granted == AutonomyLevel.A0
    assert "anonymous" in outcome.reasons[0]


# ------------------------------------------------------------------- blocked path


async def test_a_blocked_request_never_reaches_the_model() -> None:
    async def deny(state: AgentState) -> GateOutcome:
        del state
        return GateOutcome(allow=False, reasons=("dlp: secreto detectado en la entrada",))

    transport = FakeTransport()
    app = build_graph(make_deps(gateway=make_gateway(transport), governance=deny))

    final = await run(app, make_state("mi api key es sk-123"))

    assert final.status == "blocked"
    assert "dlp" in (final.blocked_reason or "")
    assert transport.calls == []
    assert "No puedo atender esta peticion" in final.answer


# -------------------------------------------------------------------------- HITL


async def test_a2_action_pauses_the_graph_until_a_human_decides() -> None:
    executed: list[str] = []

    async def executor(tool: str, args: dict[str, Any], state: AgentState) -> dict[str, Any]:
        del args, state
        executed.append(tool)
        return {"ok": True}

    deps = make_deps(
        subgraph=ItSupportSubgraph(),
        tool_catalog=[TICKET_TOOL],
        tool_executor=executor,
        autonomy_map=AutonomyMap(
            default=AutonomyLevel.A1, overrides={"gestionar_accesos": AutonomyLevel.A2}
        ),
    )
    app = build_graph(deps, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "hitl-1"}}

    paused = await app.ainvoke(make_state("necesito acceso al ERP"), config)

    # The task is suspended, the tool has not run, and there is an interrupt to answer.
    assert executed == []
    assert paused["__interrupt__"]
    payload = paused["__interrupt__"][0].value
    assert payload["type"] == "approval_required"
    assert payload["autonomy_required"] == "A2"

    resumed = await app.ainvoke(Command(resume={"approved": True, "approver": "lider-1"}), config)
    final = AgentState.model_validate(resumed)

    assert executed == ["jira.create_ticket"]
    assert final.status == "completed"
    assert final.approvals[0].approvals == ("lider-1",)


async def test_rejected_action_does_not_run_and_is_recorded() -> None:
    executed: list[str] = []

    async def executor(tool: str, args: dict[str, Any], state: AgentState) -> dict[str, Any]:
        del args, state
        executed.append(tool)
        return {"ok": True}

    deps = make_deps(
        subgraph=ItSupportSubgraph(),
        tool_catalog=[TICKET_TOOL],
        tool_executor=executor,
        autonomy_map=AutonomyMap(
            default=AutonomyLevel.A1, overrides={"gestionar_accesos": AutonomyLevel.A3}
        ),
    )
    app = build_graph(deps, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "hitl-2"}}

    await app.ainvoke(make_state("necesito acceso al ERP"), config)
    resumed = await app.ainvoke(Command(resume={"approved": False, "approver": "lider-1"}), config)
    final = AgentState.model_validate(resumed)

    assert executed == []
    assert final.tool_calls[0].ok is False
    assert final.tool_calls[0].error == "rejected by approver"
    assert final.approvals[0].rejected_by == "lider-1"


async def test_paused_task_survives_a_new_application_object() -> None:
    """Resume is the checkpointer's job, not the process's: DoD "resume after a crash"."""
    saver = InMemorySaver()
    executed: list[str] = []

    async def executor(tool: str, args: dict[str, Any], state: AgentState) -> dict[str, Any]:
        del args, state
        executed.append(tool)
        return {"ok": True}

    def fresh_app() -> Any:
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

    config = {"configurable": {"thread_id": "crash-1"}}
    await fresh_app().ainvoke(make_state("necesito acceso al ERP"), config)

    # Everything in-process is discarded; only the checkpointer survives.
    resumed = await fresh_app().ainvoke(
        Command(resume={"approved": True, "approver": "lider-1"}), config
    )
    final = AgentState.model_validate(resumed)

    assert executed == ["jira.create_ticket"]
    assert final.status == "completed"


async def test_missing_connector_registry_is_reported_not_faked() -> None:
    deps = make_deps(subgraph=ItSupportSubgraph(), tool_catalog=[TICKET_TOOL])
    app = build_graph(deps)

    final = await run(app, make_state("necesito acceso al ERP"))

    assert final.tool_calls[0].ok is False
    assert "no connector registry" in (final.tool_calls[0].error or "")


# ------------------------------------------------------------------ quality gate


async def test_failing_judge_replans_then_escalates_when_out_of_retries() -> None:
    calls = 0

    async def always_fail(state: AgentState) -> QualityVerdict:
        nonlocal calls
        calls += 1
        del state
        return QualityVerdict(verdict="retry", scores={"groundedness": 0.1})

    app = build_graph(make_deps(quality=always_fail, max_retries=1))

    final = await run(app, make_state("pregunta"))

    assert calls == 2  # first attempt plus one retry
    assert final.retries == 1
    assert final.verdict == "escalate"
    assert final.status == "awaiting_approval"


async def test_passing_judge_approves_without_replanning() -> None:
    async def pass_all(state: AgentState) -> QualityVerdict:
        del state
        return QualityVerdict(verdict="approve", scores={"groundedness": 0.95})

    app = build_graph(make_deps(quality=pass_all))

    final = await run(app, make_state("pregunta"))

    assert final.verdict == "approve"
    assert final.retries == 0
    assert final.status == "completed"


# ---------------------------------------------------------------- domain plugin


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("el servidor no funciona", "incident"),
        ("necesito acceso al ERP", "access_request"),
        ("como configuro el VPN", "how_to"),
        ("cuanto cuesta la licencia", "question"),
    ],
)
def test_it_support_detects_intent(text: str, expected: str) -> None:
    assert ItSupportSubgraph().detect_intent(text) == expected


async def test_it_support_tags_the_action_category_so_autonomy_can_find_it() -> None:
    deps = make_deps(subgraph=ItSupportSubgraph(), tool_catalog=[TICKET_TOOL])
    app = build_graph(deps)

    final = await run(app, make_state("necesito acceso al ERP"))

    assert final.plan[0].action_category == "gestionar_accesos"
    assert final.scratchpad["it_support_intent"] == "access_request"


async def test_external_backend_is_used_for_low_classification() -> None:
    """Sanity check on the other side of the invariant: C1 may go external."""
    transport = FakeTransport()
    deps = make_deps(gateway=make_gateway(transport))
    deps.model_quality = "anthropic/claude"
    app = build_graph(deps)

    final = await run(app, make_state("hola"))

    assert final.classification == Classification.C0
    assert transport.models_used == ["anthropic/claude"]
    assert deps.gateway is not None
    assert deps.gateway.policy.get("anthropic/claude").sovereignty is Sovereignty.EXTERNAL


async def test_anonymous_caller_cannot_run_even_an_a1_action() -> None:
    """The grant caps what runs without a human, not just what A2+ requires."""
    executed: list[str] = []

    async def executor(tool: str, args: dict[str, Any], state: AgentState) -> dict[str, Any]:
        del args, state
        executed.append(tool)
        return {"ok": True}

    deps = make_deps(
        subgraph=ItSupportSubgraph(),
        tool_catalog=[TICKET_TOOL],
        tool_executor=executor,
        autonomy_map=AutonomyMap(default=AutonomyLevel.A1),
    )
    app = build_graph(deps, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "anon-1"}}

    paused = await app.ainvoke(
        make_state("necesito acceso al ERP", user_id=None, authenticated=False), config
    )

    assert executed == []
    assert paused["__interrupt__"]
    assert paused["__interrupt__"][0].value["autonomy_required"] == "A1"
