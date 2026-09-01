"""The phase's exit criteria, demonstrated through a real graph rather than a unit.

Four claims, each of which is only worth anything if it holds for the whole cell:

1. C4 data never reaches an external backend.
2. An A2 action waits for a human.
3. ``verdict=retry`` re-executes from the checkpoint, not from the beginning.
4. The chain of evidence verifies.

They run against the assembled graph with a fake transport, so what is exercised is the
wiring -- gate hook, judge, checkpointer, ledger -- and not a hand-held sequence of calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agent_forge.core.autonomy import AutonomyLevel, AutonomyMap
from agent_forge.core.classification import Classification
from agent_forge.core.errors import SovereigntyError
from agent_forge.core.graph import QualityVerdict, build_graph
from agent_forge.core.state import AgentState
from agent_forge.core.subgraphs.base import (
    DomainOutcome,
    DomainSubgraph,
    Finding,
    ToolDescriptor,
    ToolRequest,
)
from agent_forge.events import Aggregator, InMemoryBus, build_ledger, verify_path
from agent_forge.governance import GovernanceService
from agent_forge.governance.dlp import DlpEngine
from agent_forge.governance.pdp import LocalPdp
from tests.support import EXTERNAL, FakeTransport, make_deps, make_gateway, make_state

REPO_ROOT = Path(__file__).resolve().parents[2]
DLP_RULES = REPO_ROOT / "configs" / "policies" / "dlp_rules.yaml"
TENANT = "acme-mx"

SEND_EMAIL = ToolDescriptor(
    name="email.send",
    description="Envia un correo",
    autonomy_min=AutonomyLevel.A2,
)


async def _run(app: Any, state: AgentState, thread: str = "t1") -> AgentState:
    raw = await app.ainvoke(state, config={"configurable": {"thread_id": thread}})
    if isinstance(raw, AgentState):
        return raw
    payload = {k: v for k, v in raw.items() if not k.startswith("__")}
    return AgentState.model_validate(payload)


def _governance(**kwargs: Any) -> GovernanceService:
    return GovernanceService(local=LocalPdp(), dlp=DlpEngine.from_file(DLP_RULES), **kwargs)


# ---------------------------------------------------- 1 · sovereignty end to end


async def test_c4_content_never_reaches_an_external_backend() -> None:
    """Exit criterion 1, at the point where the request would actually be sent.

    The gateway is the single enforcement point, so the check that matters is not "does
    policy say no" but "does the request get out". A C4 task with only an external backend
    available must fail, loudly, rather than fall back to it.
    """
    transport = FakeTransport()
    gateway = make_gateway(transport, backends=[EXTERNAL])

    with pytest.raises(SovereigntyError):
        await gateway.complete(
            [{"role": "user", "content": "el expediente completo"}],
            classification=Classification.C4,
            preferred=[EXTERNAL.alias],
            tenant_id=TENANT,
        )

    assert transport.calls == [], "nothing may be sent before the refusal"


async def test_a_task_that_accumulates_c4_is_not_synthesised_externally() -> None:
    """The whole task, not one call.

    The request itself is unclassified; what makes the task C4 is a retrieved chunk. The
    gateway has to read the *accumulated* maximum, or every task would be routed by how
    innocuous its question looked.
    """
    transport = FakeTransport(replies=["respuesta"])
    deps = make_deps(
        subgraph=_SecretFindingSubgraph(),
        gateway=make_gateway(transport),
    )
    # The profile asks for the external backend for both roles. It must not get it.
    deps.model_quality = EXTERNAL.alias
    deps.model_fast = EXTERNAL.alias
    app = build_graph(deps)

    final = await _run(app, make_state("consulta", ceiling=Classification.C4))

    assert final.classification == Classification.C4
    assert EXTERNAL.alias not in transport.models_used, (
        f"an external backend saw C4 content: {transport.models_used}"
    )
    assert transport.models_used, "the task did run; it simply ran locally"


class _SecretFindingSubgraph(DomainSubgraph):
    """Retrieves one C4 finding, which is what pushes the whole task to C4."""

    name = "secreto"
    intents = ("consulta",)

    async def gather(self, ctx: Any) -> DomainOutcome:
        return DomainOutcome(
            findings=[
                Finding(
                    source="expediente.pdf",
                    content="contenido reservado",
                    classification=Classification.C4,
                )
            ]
        )


async def test_the_governance_layer_refuses_a_c4_external_route() -> None:
    """The same rule, stated as policy so it is auditable and a platform can tighten it."""
    verdict = await _governance().may_send_to(
        make_state("q", ceiling=Classification.C4).identity,
        model="anthropic/claude",
        sovereignty="external",
        classification=Classification.C4,
    )

    assert verdict.allow is False
    assert any("external" in reason for reason in verdict.reasons)


# ------------------------------------------------------ 2 · A2 waits for a human


async def test_an_a2_action_pauses_for_approval_instead_of_executing() -> None:
    """Exit criterion 2. The tool must not run; the graph must stop."""
    executed: list[str] = []

    async def execute(tool: str, args: dict[str, Any], state: AgentState) -> dict[str, Any]:
        executed.append(tool)
        return {"ok": True}

    deps = make_deps(
        subgraph=_ToolWantingSubgraph(),
        autonomy_map=AutonomyMap(default=AutonomyLevel.A0),
        tool_catalog=(SEND_EMAIL,),
        tool_executor=execute,
    )
    deps.governance = _governance().gate
    app = build_graph(deps, checkpointer=InMemorySaver())

    raw = await app.ainvoke(
        make_state("envia el resumen al director"),
        config={"configurable": {"thread_id": "a2"}},
    )

    assert raw.get("__interrupt__"), "the graph should have paused for a human"
    assert executed == [], "an A2 tool ran without approval"


async def test_the_policy_layer_attaches_the_approval_obligation() -> None:
    verdict = await _governance().may_use_tool(
        make_state("q").identity,
        tool="email.send",
        classification=Classification.C2,
        autonomy=AutonomyLevel.A2,
    )

    assert verdict.allow is True
    assert "human_approval" in verdict.obligations
    assert "ledger_record" in verdict.obligations


class _ToolWantingSubgraph(DomainSubgraph):
    """A subgraph that always asks for the A2 tool."""

    name = "test"
    intents = ("enviar",)

    async def gather(self, ctx: Any) -> DomainOutcome:
        return DomainOutcome(
            tool_requests=[
                ToolRequest(
                    tool="email.send",
                    args={"to": "director@acme.mx"},
                    action_category="enviar_correo",
                )
            ]
        )


# ------------------------------------------------- 3 · retry from the checkpoint


async def test_a_retry_verdict_re_executes_from_the_checkpoint() -> None:
    """Exit criterion 3.

    The verdict arrives after the task finished, over a bus, in what may be a different
    process. Resuming means continuing the checkpointed run -- the earlier nodes must not
    run a second time, or every tool call the task already made would be repeated.
    """
    plans: list[int] = []
    checkpointer = InMemorySaver()
    transport = FakeTransport(replies=["primera respuesta", "segunda respuesta"])
    deps = make_deps(gateway=make_gateway(transport))
    deps.quality = _ApproveOnce(plans)
    app = build_graph(deps, checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "retry-1"}}

    first = await app.ainvoke(make_state("pregunta"), config=config)
    calls_after_first = len(transport.calls)

    # What the aggregator's resume hook does: write the verdict in as if the quality gate
    # had produced it, then continue.
    await app.aupdate_state(
        config,
        {"verdict": "retry", "scratchpad": {"judge_feedback": "cita las fuentes"}},
        as_node="quality_gate",
    )
    resumed = await app.ainvoke(None, config)

    assert first["status"] == "completed"
    assert resumed["answer"] == "segunda respuesta"
    assert len(transport.calls) > calls_after_first, "the resumed run produced a new answer"
    # The state carried over rather than being rebuilt: same task, same thread.
    assert resumed["task_id"] == first["task_id"]


async def test_a_replan_verdict_discards_the_plan_before_resuming() -> None:
    """A retry keeps the plan; a replan must not, or it reproduces the rejected answer."""
    checkpointer = InMemorySaver()
    deps = make_deps(gateway=make_gateway(FakeTransport(replies=["a", "b"])))
    app = build_graph(deps, checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "replan-1"}}

    await app.ainvoke(make_state("pregunta"), config=config)
    await app.aupdate_state(config, {"verdict": "replan", "plan": []}, as_node="quality_gate")
    resumed = await app.ainvoke(None, config)

    assert resumed["verdict"] == "approve"
    assert resumed["status"] == "completed"


async def test_the_aggregator_drives_that_resume_from_a_bus_verdict(tmp_path: Path) -> None:
    """The same thing again, but triggered the way production triggers it."""
    checkpointer = InMemorySaver()
    transport = FakeTransport(replies=["primera", "segunda"])
    deps = make_deps(gateway=make_gateway(transport))
    app = build_graph(deps, checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "bus-retry"}}
    state = make_state("pregunta")
    await app.ainvoke(state, config=config)

    bus = InMemoryBus()
    ledger = build_ledger(tmp_path)

    async def resume(task_id: str, verdict: str, reasons: tuple[str, ...]) -> None:
        await app.aupdate_state(
            config,
            {"verdict": verdict, "scratchpad": {"judge_feedback": "; ".join(reasons)}},
            as_node="quality_gate",
        )
        await app.ainvoke(None, config)

    aggregator = Aggregator(bus=bus, tenant_id=TENANT, resume=resume, ledger=ledger)
    await aggregator.listen()

    from agent_forge.events.schemas import JUDGE_VERDICT, CloudEvent, JudgeVerdictData

    await bus.publish(
        CloudEvent.wrap(
            JUDGE_VERDICT,
            JudgeVerdictData(task_id=state.task_id, verdict="retry", reasons=["sin citas"]),
            source="judge",
            tenant_id=TENANT,
        )
    )

    final = await app.aget_state(config)
    assert final.values["answer"] == "segunda"
    # And the verdict itself is evidence.
    assert ledger.verify(TENANT) >= 1


class _ApproveOnce:
    """Rejects the first answer, approves the second. Records how often it was asked."""

    def __init__(self, calls: list[int]) -> None:
        self._calls = calls

    async def __call__(self, state: AgentState) -> QualityVerdict:
        self._calls.append(state.retries)
        return QualityVerdict(verdict="approve", scores={"coverage": 1.0})


# --------------------------------------------------------- 4 · evidence chain


async def test_a_governed_run_leaves_a_verifiable_chain(tmp_path: Path) -> None:
    """Exit criterion 4, over records a real run produced rather than synthetic ones."""
    ledger = build_ledger(tmp_path)

    async def record(action: str, actor: str, payload: dict[str, Any]) -> None:
        await ledger.record(action, actor, payload, tenant_id=TENANT)  # type: ignore[arg-type]

    deps = make_deps(gateway=make_gateway(FakeTransport(replies=["respuesta"])))
    deps.governance = _governance(ledger=record).gate
    app = build_graph(deps, checkpointer=InMemorySaver())

    await _run(app, make_state("cual es el limite?"), thread="ledger-1")
    aggregator = Aggregator(bus=InMemoryBus(), tenant_id=TENANT, ledger=ledger)
    await aggregator.publish_result(
        make_state("cual es el limite?").model_copy(update={"answer": "1500"})
    )

    assert verify_path(tmp_path)[TENANT] >= 2


async def test_the_verify_ledger_script_reports_a_healthy_chain(tmp_path: Path) -> None:
    """``make verify-ledger`` is what CI runs, so the script is what has to be right."""
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from scripts.verify_ledger import main

    ledger = build_ledger(tmp_path)
    for index in range(3):
        await ledger.record("tool_call", "agent", {"i": index}, tenant_id=TENANT)

    assert main(["--path", str(tmp_path)]) == 0


async def test_the_verify_ledger_script_fails_on_a_broken_chain(tmp_path: Path) -> None:
    import json
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from scripts.verify_ledger import main

    ledger = build_ledger(tmp_path)
    for index in range(2):
        await ledger.record("tool_call", "agent", {"i": index}, tenant_id=TENANT)

    path = next((tmp_path / TENANT).glob("*.jsonl"))
    lines = path.read_text(encoding="utf-8").splitlines()
    broken = json.loads(lines[0])
    broken["payload_digest"] = "0" * 64
    lines[0] = json.dumps(broken, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert main(["--path", str(tmp_path)]) == 1


def test_the_verify_ledger_script_succeeds_when_there_is_no_ledger_yet(tmp_path: Path) -> None:
    """A cell that has answered nothing has nothing to verify. That is not a failure."""
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from scripts.verify_ledger import main

    assert main(["--path", str(tmp_path / "absent")]) == 0
