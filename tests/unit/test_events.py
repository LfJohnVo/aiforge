"""The event fabric, the local judge, and the evidence chain.

Three of the phase's four exit criteria live in this module: ``verdict=retry`` re-executes
from the checkpoint, the chain verifies, and a tampered chain does not. The fourth -- C4
never reaching an external backend -- is in ``test_governance_e2e.py``, where it can be
shown end to end rather than as a policy lookup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_forge.core.classification import Classification
from agent_forge.core.state import AgentState, Citation, Identity, Message, Usage
from agent_forge.events import (
    JUDGE_VERDICT,
    TASK_RESULT,
    Aggregator,
    ChainError,
    CloudEvent,
    EvidenceLedger,
    InMemoryBus,
    JsonlSink,
    JudgeVerdictData,
    LocalJudge,
    build_bus,
    build_ledger,
    digest,
    result_event,
    subject_for,
    verify_chain,
    verify_path,
)
from agent_forge.events.bus import SeenEvents
from agent_forge.events.evidence import GENESIS_HASH, LedgerRecord

TENANT = "acme-mx"
DLP_RULES = Path(__file__).resolve().parents[2] / "configs" / "policies" / "dlp_rules.yaml"


def _escaped(root: Path) -> list[Path]:
    """Anything the ledger wrote outside its own root. Must always be empty."""
    written = list(root.rglob("*.jsonl"))
    assert written, "the record went somewhere; the question is where"
    return [p for p in written if root not in p.parents]


def _ledger_text(root: Path) -> str:
    """Read a tenant's ledger file. Outside the async tests, where pathlib blocks."""
    return next((root / TENANT).glob("*.jsonl")).read_text(encoding="utf-8")


def _state(**kwargs: Any) -> AgentState:
    base: dict[str, Any] = {
        "identity": Identity(
            tenant_id=TENANT,
            user_id="u1",
            groups=("finanzas",),
            classification_ceiling=Classification.C2,
            authenticated=True,
        ),
        "agent_name": "asistente-finanzas",
        "area": "finanzas",
        "trace_id": "00-trace-span-01",
        "answer": "El limite es 1500 MXN.",
        "status": "completed",
        "classification": Classification.C2,
        "messages": [Message(role="user", content="cual es el limite?")],
        "citations": [Citation(source_id="policy.pdf", chunk_id="p1")],
        "usage": Usage(tokens_in=100, tokens_out=20, cost_usd=0.001),
    }
    base.update(kwargs)
    return AgentState(**base)


# ------------------------------------------------------------------- subjects


def test_the_tenant_is_part_of_the_subject_not_only_the_payload() -> None:
    """A NATS consumer filters on subject; this is what isolates one tenant from another."""
    assert subject_for(TENANT, TASK_RESULT) == "peak.acme-mx.com-peak-task-result-v1"


def test_dots_in_the_event_type_are_flattened() -> None:
    """NATS reads ``.`` as a separator, so an unflattened type breaks wildcard matching."""
    assert subject_for("t", "a.b.c").count(".") == 2


def test_a_tenantless_event_still_gets_a_valid_subject() -> None:
    assert subject_for("", TASK_RESULT).startswith("peak._.")


# ------------------------------------------------------------------------ bus


async def test_a_subscriber_receives_its_tenants_events() -> None:
    bus = InMemoryBus()
    received: list[CloudEvent] = []
    await bus.subscribe(TASK_RESULT, lambda e: _collect(received, e), tenant_id=TENANT)

    await bus.publish(result_event(_state(), source="test"))

    assert len(received) == 1


async def test_a_subscriber_does_not_receive_another_tenants_events() -> None:
    bus = InMemoryBus()
    received: list[CloudEvent] = []
    await bus.subscribe(TASK_RESULT, lambda e: _collect(received, e), tenant_id="otro-tenant")

    await bus.publish(result_event(_state(), source="test"))

    assert received == []


async def test_a_redelivered_event_is_handled_once() -> None:
    """At-least-once delivery means a retry verdict can arrive twice; acting twice is wrong."""
    bus = InMemoryBus()
    received: list[CloudEvent] = []
    await bus.subscribe(TASK_RESULT, lambda e: _collect(received, e), tenant_id=TENANT)
    event = result_event(_state(), source="test")

    await bus.publish(event)
    await bus.publish(event)

    assert len(received) == 1


async def test_a_handler_that_keeps_failing_ends_in_the_dead_letter_queue() -> None:
    bus = InMemoryBus(max_deliveries=2)

    async def always_fails(event: CloudEvent) -> None:
        raise RuntimeError("consumer is broken")

    await bus.subscribe(TASK_RESULT, always_fails, tenant_id=TENANT)
    await bus.publish(result_event(_state(), source="test"))

    assert len(bus.dead_letters) == 1


async def test_one_failing_handler_does_not_stop_the_publisher() -> None:
    bus = InMemoryBus(max_deliveries=1)
    received: list[CloudEvent] = []

    async def always_fails(event: CloudEvent) -> None:
        raise RuntimeError("boom")

    await bus.subscribe(TASK_RESULT, always_fails, tenant_id=TENANT)
    await bus.subscribe(TASK_RESULT, lambda e: _collect(received, e), tenant_id=TENANT)

    await bus.publish(result_event(_state(), source="test"))

    assert len(received) == 1


def test_the_seen_set_is_bounded() -> None:
    seen = SeenEvents(capacity=3)

    for index in range(5):
        seen.add(f"e{index}")

    assert len(seen) == 3
    assert "e0" not in seen


def test_no_fabric_url_yields_the_in_process_bus() -> None:
    assert isinstance(build_bus(""), InMemoryBus)


# -------------------------------------------------------------- task.result


def test_the_result_event_carries_a_digest_not_the_answer() -> None:
    """The fabric is shared infrastructure; the aggregator correlates, it does not read."""
    event = result_event(_state(), source="agent-forge")

    payload = json.dumps(event.model_dump())
    assert "El limite es 1500 MXN." not in payload
    assert event.data["output"]["answer_digest"] == digest("El limite es 1500 MXN.")


def test_the_result_event_matches_the_contract_fields() -> None:
    event = result_event(_state(), source="agent-forge")

    assert event.type == TASK_RESULT
    assert event.specversion == "1.0"
    assert {"task_id", "trace_id", "tenant_id", "agent_id", "area"} <= set(event.data)
    assert {"latency_ms", "tokens_in", "tokens_out", "cost_usd"} <= set(event.data["metrics"])


def test_an_unfinished_state_reports_as_failed_not_as_a_new_status() -> None:
    """The aggregator's vocabulary has three words; inventing a fourth breaks consumers."""
    event = result_event(_state(status="blocked"), source="t")

    assert event.data["status"] == "failed"


def test_an_awaiting_approval_state_says_so() -> None:
    event = result_event(_state(status="awaiting_approval"), source="t")

    assert event.data["status"] == "awaiting_approval"


# -------------------------------------------------------------- aggregator


async def test_an_approve_verdict_does_nothing() -> None:
    resumed: list[tuple[str, str]] = []
    aggregator = Aggregator(bus=InMemoryBus(), tenant_id=TENANT, resume=_recorder(resumed))

    await aggregator.on_verdict(_verdict_event("t1", "approve"))

    assert resumed == []


async def test_a_retry_verdict_resumes_the_task() -> None:
    resumed: list[tuple[str, str]] = []
    aggregator = Aggregator(bus=InMemoryBus(), tenant_id=TENANT, resume=_recorder(resumed))

    await aggregator.on_verdict(_verdict_event("t1", "retry"))

    assert resumed == [("t1", "retry")]


async def test_an_escalate_verdict_raises_an_approval_rather_than_retrying() -> None:
    resumed: list[tuple[str, str]] = []
    escalated: list[str] = []

    async def escalate(task_id: str, reasons: tuple[str, ...]) -> None:
        escalated.append(task_id)

    aggregator = Aggregator(
        bus=InMemoryBus(), tenant_id=TENANT, resume=_recorder(resumed), escalate=escalate
    )

    await aggregator.on_verdict(_verdict_event("t1", "escalate"))

    assert escalated == ["t1"]
    assert resumed == []


async def test_a_verdict_arriving_over_the_bus_reaches_the_aggregator() -> None:
    bus = InMemoryBus()
    resumed: list[tuple[str, str]] = []
    aggregator = Aggregator(bus=bus, tenant_id=TENANT, resume=_recorder(resumed))
    await aggregator.listen()

    await bus.publish(_verdict_event("t1", "replan"))

    assert resumed == [("t1", "replan")]


async def test_publishing_a_result_also_records_it_in_the_ledger(tmp_path: Path) -> None:
    ledger = build_ledger(tmp_path)
    aggregator = Aggregator(bus=InMemoryBus(), tenant_id=TENANT, ledger=ledger)

    await aggregator.publish_result(_state())

    assert ledger.verify(TENANT) == 1


def _verdict_event(task_id: str, verdict: str) -> CloudEvent:
    return CloudEvent.wrap(
        JUDGE_VERDICT,
        JudgeVerdictData(task_id=task_id, verdict=verdict, score=0.4, reasons=["r"]),
        source="judge",
        tenant_id=TENANT,
    )


def _recorder(sink: list[tuple[str, str]]) -> Any:
    async def resume(task_id: str, verdict: str, reasons: tuple[str, ...]) -> None:
        sink.append((task_id, verdict))

    return resume


async def _collect(sink: list[CloudEvent], event: CloudEvent) -> None:
    sink.append(event)


# -------------------------------------------------------------- the ledger


async def test_the_chain_verifies_after_a_run_of_records(tmp_path: Path) -> None:
    """Exit criterion: the evidence chain verifies."""
    ledger = build_ledger(tmp_path)

    for index in range(5):
        await ledger.record(
            "tool_call", "agent", {"tool": f"t{index}"}, tenant_id=TENANT, metadata={"i": index}
        )

    assert ledger.verify(TENANT) == 5
    assert verify_path(tmp_path) == {TENANT: 5}


async def test_the_first_record_points_at_the_genesis_hash(tmp_path: Path) -> None:
    ledger = build_ledger(tmp_path)

    record = await ledger.record("policy_decision", "agent", {"allow": True}, tenant_id=TENANT)

    assert record.seq == 1
    assert record.prev_hash == GENESIS_HASH


async def test_each_record_points_at_the_one_before_it(tmp_path: Path) -> None:
    ledger = build_ledger(tmp_path)

    first = await ledger.record("a", "agent", {}, tenant_id=TENANT)
    second = await ledger.record("b", "agent", {}, tenant_id=TENANT)

    assert second.prev_hash == first.hash


async def test_editing_a_record_breaks_the_chain(tmp_path: Path) -> None:
    """The point of the chain: a rewrite is detectable, and named."""
    ledger = build_ledger(tmp_path)
    for index in range(3):
        await ledger.record("tool_call", "agent", {"i": index}, tenant_id=TENANT)

    path = next((tmp_path / TENANT).glob("*.jsonl"))
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["action"] = "approval"
    lines[1] = json.dumps(tampered, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ChainError) as caught:
        verify_path(tmp_path)

    assert caught.value.seq == 2
    assert "modified" in str(caught.value)


async def test_removing_a_record_breaks_the_chain(tmp_path: Path) -> None:
    ledger = build_ledger(tmp_path)
    for index in range(3):
        await ledger.record("tool_call", "agent", {"i": index}, tenant_id=TENANT)

    path = next((tmp_path / TENANT).glob("*.jsonl"))
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

    with pytest.raises(ChainError, match="missing"):
        verify_path(tmp_path)


async def test_editing_the_metadata_alone_breaks_the_chain(tmp_path: Path) -> None:
    """Metadata is inside the hash: the tool name is exactly what somebody would edit."""
    ledger = build_ledger(tmp_path)
    await ledger.record("tool_call", "agent", {}, tenant_id=TENANT, metadata={"tool": "read"})

    path = next((tmp_path / TENANT).glob("*.jsonl"))
    record = json.loads(path.read_text(encoding="utf-8").strip())
    record["metadata"] = {"tool": "delete"}
    path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", "utf-8")

    with pytest.raises(ChainError, match="modified"):
        verify_path(tmp_path)


async def test_the_ledger_stores_a_digest_and_never_the_payload(tmp_path: Path) -> None:
    """A ledger is kept for years and read by people not entitled to the content."""
    ledger = build_ledger(tmp_path)
    secret = "el limite del director general es 90000 MXN"

    await ledger.record("prompt", "agent", {"text": secret}, tenant_id=TENANT)

    written = _ledger_text(tmp_path)
    assert secret not in written
    assert digest({"text": secret}) in written


async def test_two_tenants_keep_independent_chains(tmp_path: Path) -> None:
    ledger = build_ledger(tmp_path)

    await ledger.record("a", "agent", {}, tenant_id="uno")
    await ledger.record("a", "agent", {}, tenant_id="dos")
    second_for_uno = await ledger.record("b", "agent", {}, tenant_id="uno")

    assert second_for_uno.seq == 2
    assert verify_path(tmp_path) == {"dos": 1, "uno": 2}


async def test_a_restart_resumes_the_chain_instead_of_starting_a_new_one(
    tmp_path: Path,
) -> None:
    """Two records with seq 1 look exactly like tampering to a verifier."""
    await build_ledger(tmp_path).record("a", "agent", {}, tenant_id=TENANT)

    reopened = build_ledger(tmp_path)
    second = await reopened.record("b", "agent", {}, tenant_id=TENANT)

    assert second.seq == 2
    assert reopened.verify(TENANT) == 2


async def test_a_tenant_id_cannot_escape_the_ledger_directory(tmp_path: Path) -> None:
    ledger = build_ledger(tmp_path)

    await ledger.record("a", "agent", {}, tenant_id="../../etc")

    assert _escaped(tmp_path) == []


async def test_concurrent_records_do_not_fork_the_chain(tmp_path: Path) -> None:
    """A fork fails verification in a way that looks exactly like tampering."""
    import asyncio

    ledger = build_ledger(tmp_path)

    await asyncio.gather(
        *(ledger.record("a", "agent", {"i": i}, tenant_id=TENANT) for i in range(20))
    )

    assert ledger.verify(TENANT) == 20


async def test_the_export_is_verified_as_it_is_produced(tmp_path: Path) -> None:
    ledger = build_ledger(tmp_path)
    for index in range(3):
        await ledger.record("a", "agent", {"i": index}, tenant_id=TENANT)

    exported = list(ledger.export(TENANT))

    assert len(exported) == 3
    assert json.loads(exported[0])["seq"] == 1


async def test_the_ledger_mirrors_records_onto_the_fabric(tmp_path: Path) -> None:
    bus = InMemoryBus()
    ledger = build_ledger(tmp_path, bus=bus)

    await ledger.record("approval", "human", {"approved": True}, tenant_id=TENANT)

    published = bus.events_of("com.peak.evidence.record.v1")
    assert len(published) == 1
    assert published[0].data["actor"] == "human"


async def test_a_fabric_that_is_down_does_not_stop_the_local_record(tmp_path: Path) -> None:
    """The local chain is the source of truth; the central ledger is a mirror."""

    class _Broken:
        async def publish(self, event: CloudEvent) -> None:
            raise ConnectionError("fabric is down")

    ledger = EvidenceLedger(JsonlSink(tmp_path), bus=_Broken())

    record = await ledger.record("a", "agent", {}, tenant_id=TENANT)

    assert record.seq == 1
    assert ledger.verify(TENANT) == 1


def test_an_empty_chain_verifies_as_zero_records(tmp_path: Path) -> None:
    assert verify_chain(iter([])) == 0


def test_a_hash_covers_every_field_of_the_record() -> None:
    """If a field is outside the hash, that field can be edited freely."""
    fields = {
        "seq": 1,
        "prev_hash": GENESIS_HASH,
        "ts": "2026-09-01T00:00:00+00:00",
        "tenant_id": TENANT,
        "actor": "agent",
        "action": "tool_call",
        "payload_digest": digest({}),
        "metadata": {"tool": "read"},
    }
    baseline = LedgerRecord.compute_hash(**fields)  # type: ignore[arg-type]

    for name, altered in (
        ("seq", 2),
        ("prev_hash", "f" * 64),
        ("ts", "2026-09-02T00:00:00+00:00"),
        ("tenant_id", "otro"),
        ("actor", "human"),
        ("action", "approval"),
        ("payload_digest", digest({"x": 1})),
        ("metadata", {"tool": "delete"}),
    ):
        assert LedgerRecord.compute_hash(**{**fields, name: altered}) != baseline, name  # type: ignore[arg-type]


# --------------------------------------------------------------- local judge


async def test_the_judge_approves_a_grounded_answer() -> None:
    verdict = await LocalJudge().judge(_state())

    assert verdict.verdict == "approve"


async def test_the_judge_retries_an_empty_answer() -> None:
    verdict = await LocalJudge().judge(_state(answer="   "))

    assert verdict.verdict == "retry"


async def test_the_judge_rejects_an_answer_that_ignores_what_was_retrieved() -> None:
    state = _state(citations=[], scratchpad={"findings": ["algo relevante"]})

    verdict = await LocalJudge().judge(state)

    assert verdict.verdict == "replan"
    assert verdict.scores["coverage"] == 0.0


async def test_the_judge_accepts_an_uncited_answer_when_nothing_was_retrieved() -> None:
    """A greeting needs no source; demanding one would make the cell unusable."""
    verdict = await LocalJudge().judge(_state(citations=[], answer="Hola, en que te ayudo?"))

    assert verdict.verdict == "approve"


async def test_the_judge_escalates_when_the_answer_leaks_a_secret() -> None:
    """Not a retry: the same generation would leak again. A human decides."""
    from agent_forge.governance.dlp import DlpEngine

    engine = DlpEngine.from_file(DLP_RULES)
    judge = LocalJudge(scan_answer=lambda text: engine.scan(text, "output"))

    verdict = await judge.judge(_state(answer="la clave es AKIAIOSFODNN7EXAMPLE"))

    assert verdict.verdict == "escalate"


async def test_a_model_safety_score_below_the_threshold_escalates() -> None:
    """Safety is not a prompting problem, so retrying the same plan will not fix it."""
    judge = LocalJudge(gateway=_ScoringGateway({"groundedness": 0.99, "safety": 0.10}))

    verdict = await judge.judge(_state())

    assert verdict.verdict == "escalate"


async def test_a_model_groundedness_score_below_the_threshold_replans() -> None:
    judge = LocalJudge(gateway=_ScoringGateway({"groundedness": 0.10, "safety": 0.99}))

    verdict = await judge.judge(_state())

    assert verdict.verdict == "replan"


async def test_a_judge_whose_model_is_down_still_decides() -> None:
    """A judge that cannot run is no reason to ship an unchecked answer -- or to block one."""

    class _Down:
        async def complete(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("model is down")

    verdict = await LocalJudge(gateway=_Down()).judge(_state())

    assert verdict.verdict == "approve"
    assert verdict.scores == {"coverage": 1.0}


async def test_the_judge_grades_with_the_tasks_own_classification() -> None:
    """A C4 answer must not be sent to an external model to be graded."""
    gateway = _ScoringGateway({"groundedness": 0.99, "safety": 0.99})
    judge = LocalJudge(gateway=gateway)

    await judge.judge(_state(classification=Classification.C4))

    assert gateway.seen_classification == Classification.C4


async def test_a_chatty_model_cannot_approve_itself_by_omitting_a_criterion() -> None:
    """A missing criterion is absent, not passing."""
    judge = LocalJudge(gateway=_ScoringGateway({}, reply="Me parece una buena respuesta!"))

    verdict = await judge.judge(_state())

    # Nothing parsed, so only the deterministic layer spoke -- and it found no fault.
    assert verdict.scores == {"coverage": 1.0}


class _ScoringGateway:
    def __init__(self, scores: dict[str, float], reply: str = "") -> None:
        self._reply = reply or "\n".join(f"{k}: {v}" for k, v in scores.items())
        self.seen_classification: Classification | None = None

    async def complete(self, messages: Any, **kwargs: Any) -> Any:
        self.seen_classification = kwargs.get("classification")

        class _Reply:
            content = self._reply

        return _Reply()
