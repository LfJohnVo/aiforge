"""The HITL loop over HTTP: pause, queue, decide, resume.

DoD item 6. The unit tests in ``test_graph.py`` prove the graph pauses; these prove an
operator can actually find the paused action and release it, and that the rules about
*who* may release it hold.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_forge.api.admin import (
    ApprovalDecision,
    decide_approval,
    effective_config,
    forget,
    list_approvals,
    memory_stats,
    task_status,
)
from agent_forge.core.autonomy import AutonomyLevel, AutonomyMap
from agent_forge.core.checkpointer import namespaced_thread_id
from agent_forge.core.classification import Classification
from agent_forge.core.errors import (
    ApprovalError,
    AuthorizationError,
    TaskNotFoundError,
)
from agent_forge.core.hitl import (
    ApprovalPolicy,
    Approver,
    PendingApproval,
    apply_approval,
    apply_rejection,
    build_request,
    summarise_action,
)
from agent_forge.core.state import AgentState, Identity, Message
from agent_forge.core.subgraphs.it_support import ItSupportSubgraph
from tests.support import FakeTransport, make_state

from .test_api import TENANT, TICKET_TOOL, _runtime

APPROVER = Identity(
    tenant_id=TENANT,
    user_id="lider-1",
    groups=("finanzas-lideres",),
    classification_ceiling=Classification.C2,
    authenticated=True,
)
OUTSIDER = Identity(
    tenant_id=TENANT,
    user_id="becario-1",
    groups=("finanzas",),
    classification_ceiling=Classification.C1,
    authenticated=True,
)


def _runtime_with_a2(executed: list[str]) -> Any:
    async def executor(tool: str, args: dict[str, Any], state: AgentState) -> dict[str, Any]:
        del args, state
        executed.append(tool)
        return {"ok": True}

    return _runtime(
        FakeTransport(replies=["Ticket creado."]),
        subgraph=ItSupportSubgraph(),
        tool_catalog=(TICKET_TOOL,),
        tool_executor=executor,
        autonomy_map=AutonomyMap(
            default=AutonomyLevel.A1, overrides={"gestionar_accesos": AutonomyLevel.A2}
        ),
    )


async def _pause_a_task(runtime: Any) -> PendingApproval:
    """Drive the graph until it interrupts, then read the queue."""
    from agent_forge.channels.openai_api import _invoke

    state = make_state("necesito acceso al ERP", tenant_id=TENANT)
    await _invoke(runtime, state)
    queue = await runtime.approvals.list_pending(TENANT)
    assert queue, "the graph paused but nothing reached the approval queue"
    return queue[0]


# ------------------------------------------------------------------ queue + resume


async def test_approving_resumes_the_task_and_runs_the_action() -> None:
    executed: list[str] = []
    runtime = _runtime_with_a2(executed)
    pending = await _pause_a_task(runtime)
    assert executed == []

    listed = await list_approvals(runtime, APPROVER)
    assert listed["count"] == 1
    assert listed["items"][0]["autonomy_required"] == "A2"

    result = await decide_approval(
        pending.request.id, ApprovalDecision(decision="approve"), runtime, APPROVER
    )

    assert result.status_code == 200
    assert executed == ["jira.create_ticket"]


async def test_rejecting_resumes_without_running_the_action() -> None:
    executed: list[str] = []
    runtime = _runtime_with_a2(executed)
    pending = await _pause_a_task(runtime)

    await decide_approval(
        pending.request.id,
        ApprovalDecision(decision="reject", reason="no procede"),
        runtime,
        APPROVER,
    )

    assert executed == []


async def test_an_approver_outside_the_group_cannot_decide() -> None:
    runtime = _runtime_with_a2([])
    pending = await _pause_a_task(runtime)

    with pytest.raises(ApprovalError, match="not a member"):
        await decide_approval(
            pending.request.id, ApprovalDecision(decision="approve"), runtime, OUTSIDER
        )


async def test_unknown_request_id_is_a_404() -> None:
    runtime = _runtime_with_a2([])

    with pytest.raises(TaskNotFoundError):
        await decide_approval(
            "does-not-exist", ApprovalDecision(decision="approve"), runtime, APPROVER
        )


async def test_a_credential_from_another_tenant_is_refused() -> None:
    runtime = _runtime_with_a2([])
    intruder = APPROVER.model_copy(update={"tenant_id": "otra-empresa"})

    with pytest.raises(AuthorizationError, match="different tenant"):
        await list_approvals(runtime, intruder)


async def test_task_status_reads_from_the_checkpointer() -> None:
    runtime = _runtime_with_a2([])
    pending = await _pause_a_task(runtime)

    status = await task_status(pending.task_id, pending.thread_id, runtime, APPROVER)

    assert status["status"] == "running"
    assert status["next"] == ["tools"]
    assert status["awaiting"] == []  # the request lives in the queue, not yet in state


async def test_task_status_for_an_unknown_thread_is_a_404() -> None:
    runtime = _runtime_with_a2([])

    with pytest.raises(TaskNotFoundError):
        await task_status("t", "no-such-thread", runtime, APPROVER)


async def test_forget_reports_exactly_what_was_deleted() -> None:
    """The report is evidence; it has to reflect what actually happened per layer."""
    runtime = _runtime_with_a2([])
    state = make_state("una pregunta", tenant_id=TENANT).model_copy(
        update={"status": "completed", "answer": "una respuesta"}
    )
    await runtime.memory.record_turn(
        state, user_message=Message(role="user", content="una pregunta")
    )
    await runtime.memory.remember_facts(state, ["un hecho recordado"])

    result = await forget(runtime, APPROVER, "user", "u1", [state.thread_id])

    assert result["status"] == "ok"
    assert result["deleted"]["short_term"] == 1
    assert result["deleted"]["long_term"] == 1


async def test_wiping_a_whole_tenant_requires_an_approver() -> None:
    """Cell-wide irreversible deletion is not something any authenticated user may do."""
    runtime = _runtime_with_a2([])

    with pytest.raises(AuthorizationError, match="approvers group"):
        await forget(runtime, OUTSIDER, "tenant", None, None)


async def test_memory_stats_are_reported() -> None:
    runtime = _runtime_with_a2([])

    stats = await memory_stats(runtime, APPROVER)

    assert stats["healthy"] is True
    assert stats["cache_hits"] == 0
    assert stats["retention_days"] == 365


async def test_effective_config_lists_backend_sovereignty() -> None:
    runtime = _runtime_with_a2([])

    config = await effective_config(runtime, APPROVER)

    sovereignties = {b["alias"]: b["sovereignty"] for b in config["models"]["backends"]}
    assert sovereignties["local/fast"] == "local"
    assert sovereignties["anthropic/claude"] == "external"


# --------------------------------------------------------------- approval rules


def _policy() -> ApprovalPolicy:
    return ApprovalPolicy(approvers_group="lideres", elevated_group="directores")


def test_a4_requires_two_distinct_approvers() -> None:
    request = build_request(description="borrar", autonomy_required=AutonomyLevel.A4)
    policy = ApprovalPolicy(approvers_group="lideres")
    first = Approver("ana", ("lideres",))
    second = Approver("beto", ("lideres",))

    after_first = apply_approval(request, first, policy)
    assert after_first.is_pending, "one approval must not release an A4 action"

    after_second = apply_approval(after_first, second, policy)
    assert after_second.is_granted
    assert after_second.decided_at is not None


def test_the_same_person_cannot_supply_both_a4_approvals() -> None:
    request = build_request(description="borrar", autonomy_required=AutonomyLevel.A4)
    policy = ApprovalPolicy(approvers_group="lideres")
    ana = Approver("ana", ("lideres",))

    once = apply_approval(request, ana, policy)

    with pytest.raises(ApprovalError, match="already approved"):
        apply_approval(once, ana, policy)


def test_a3_requires_the_elevated_group_when_one_is_configured() -> None:
    request = build_request(description="tocar erp", autonomy_required=AutonomyLevel.A3)
    lider = Approver("ana", ("lideres",))

    with pytest.raises(ApprovalError):
        apply_approval(request, lider, _policy())

    director = Approver("dir", ("directores",))
    assert apply_approval(request, director, _policy()).is_granted


def test_a3_falls_back_to_the_base_group_when_no_elevated_group_exists() -> None:
    """Falling back must tighten to the base group, never to nobody."""
    request = build_request(description="tocar erp", autonomy_required=AutonomyLevel.A3)
    policy = ApprovalPolicy(approvers_group="lideres")

    assert apply_approval(request, Approver("ana", ("lideres",)), policy).is_granted
    with pytest.raises(ApprovalError):
        apply_approval(request, Approver("nadie", ()), policy)


def test_one_rejection_is_final_even_for_a4() -> None:
    request = build_request(description="borrar", autonomy_required=AutonomyLevel.A4)
    policy = ApprovalPolicy(approvers_group="lideres")

    rejected = apply_rejection(request, Approver("ana", ("lideres",)), policy, "no")

    assert not rejected.is_pending
    assert not rejected.is_granted
    with pytest.raises(ApprovalError, match="already decided"):
        apply_approval(rejected, Approver("beto", ("lideres",)), policy)


def test_summarised_actions_truncate_long_values() -> None:
    """An approval queue is a UI; dumping raw arguments makes it a place to read secrets."""
    text = summarise_action("jira.create_ticket", [("body", "x" * 200)])

    assert len(text) < 120
    assert text.endswith("...")


def test_summarised_action_without_arguments() -> None:
    assert summarise_action("jira.list") == "Ejecutar `jira.list`"


def test_thread_ids_require_a_tenant() -> None:
    from agent_forge.core.checkpointer import CheckpointerError

    assert namespaced_thread_id("acme", "inst", "t") == "acme:inst:t"
    with pytest.raises(CheckpointerError):
        namespaced_thread_id("", "inst", "t")
