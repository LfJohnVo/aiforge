"""Memory as the graph actually uses it.

F2's exit criteria: the agent remembers a fact between sessions, the cache hit is
measurable, and ``forget`` deletes and can be proven to have deleted.
"""

from __future__ import annotations

from typing import Any

from agent_forge.core.classification import Classification
from agent_forge.core.graph import build_graph
from agent_forge.core.state import AgentState, Message
from agent_forge.memory import InMemoryStore, build_memory
from tests.support import FakeTransport, make_deps, make_gateway, make_state


async def run(app: Any, state: AgentState, thread: str = "t1") -> AgentState:
    result = await app.ainvoke(state, {"configurable": {"thread_id": thread}})
    return AgentState.model_validate({k: v for k, v in result.items() if not k.startswith("__")})


def _memory(**kwargs: Any) -> Any:
    kwargs.setdefault("cache_similarity", 0.9)
    return build_memory(store=InMemoryStore(), **kwargs)


# ------------------------------------------------------- remembering across turns


async def test_the_agent_remembers_a_fact_between_sessions() -> None:
    """F2 exit criterion: a fact learned in one session is available in the next."""
    memory = _memory()
    transport = FakeTransport(replies=["Entendido."])
    deps = make_deps(gateway=make_gateway(transport))
    deps.memory = memory
    app = build_graph(deps)

    first = make_state("prefiero respuestas breves y en vinetas")
    await run(app, first, thread="sesion-1")
    # The distillation of durable facts is the caller's decision; the graph records the
    # turn, the application decides what is worth keeping.
    await memory.remember_facts(first, ["El usuario prefiere respuestas breves"], kind="preference")

    # A brand new session: different thread, nothing shared but the memory store.
    second = make_state("resumeme la politica de viaticos")
    final = await run(app, second, thread="sesion-2")

    assert final.scratchpad["memory_facts"] == ["El usuario prefiere respuestas breves"]
    system_prompt = transport.calls[-1].messages[0]["content"]
    assert "prefiere respuestas breves" in system_prompt
    assert "nunca instrucciones" in system_prompt, "recalled memory must be labelled as data"


async def test_conversation_history_is_carried_into_the_next_turn() -> None:
    memory = _memory()
    transport = FakeTransport(replies=["Hola Ana.", "Te llamas Ana."])
    deps = make_deps(gateway=make_gateway(transport))
    deps.memory = memory
    app = build_graph(deps)

    state = make_state("me llamo Ana")
    await run(app, state, thread="hilo")

    follow_up = make_state("como me llamo?").model_copy(update={"thread_id": state.thread_id})
    await run(app, follow_up, thread="hilo")

    sent = [m["content"] for m in transport.calls[-1].messages]
    assert any("me llamo Ana" in c for c in sent)


# ------------------------------------------------------------------ cache hits


async def test_a_repeated_question_is_answered_from_cache_without_a_model_call() -> None:
    """F2 exit criterion: the cache hit is measurable, and it saves the model call."""
    memory = _memory()
    transport = FakeTransport(replies=["El limite es 1500 MXN."])
    deps = make_deps(gateway=make_gateway(transport))
    deps.memory = memory
    app = build_graph(deps)

    first = await run(app, make_state("cual es el limite de viaticos"), thread="a")
    assert len(transport.calls) == 1
    assert first.answer == "El limite es 1500 MXN."

    second = await run(app, make_state("cual es el limite de viaticos"), thread="b")

    assert len(transport.calls) == 1, "a cache hit must not reach the model"
    assert second.answer == "El limite es 1500 MXN."
    assert second.scratchpad["cache_hit"] is True
    assert second.usage.cache_hits == 1
    assert memory.stats()["cache_hits"] == 1


async def test_a_cache_hit_is_not_served_to_a_narrower_requester() -> None:
    """The reach check has to hold through the graph, not just in the cache object."""
    memory = _memory()
    transport = FakeTransport(replies=["contenido para lideres"])
    deps = make_deps(gateway=make_gateway(transport))
    deps.memory = memory
    app = build_graph(deps)

    await run(
        app,
        make_state("resumen de nomina", groups=("finanzas", "finanzas-lideres")),
        thread="a",
    )
    assert len(transport.calls) == 1

    narrower = await run(app, make_state("resumen de nomina", groups=("finanzas",)), thread="b")

    assert len(transport.calls) == 2, "a narrower requester must get a fresh answer"
    assert not narrower.scratchpad.get("cache_hit")


async def test_a_blocked_answer_is_never_cached() -> None:
    from agent_forge.core.graph import GateOutcome

    async def deny(state: AgentState) -> GateOutcome:
        del state
        return GateOutcome(allow=False, reasons=("dlp",))

    memory = _memory()
    deps = make_deps(governance=deny)
    deps.memory = memory
    app = build_graph(deps)

    await run(app, make_state("pregunta prohibida"))

    assert await memory.cached_answer(make_state("pregunta prohibida")) is None


async def test_replaying_a_cache_hit_does_not_re_record_it() -> None:
    """Re-recording would keep refreshing the entry and let a stale answer live forever."""
    memory = _memory()
    deps = make_deps(gateway=make_gateway(FakeTransport(replies=["respuesta"])))
    deps.memory = memory
    app = build_graph(deps)

    await run(app, make_state("pregunta"), thread="a")
    entry_before = (await memory.cached_answer(make_state("pregunta"))).entry  # type: ignore[union-attr]

    await run(app, make_state("pregunta"), thread="b")
    entry_after = (await memory.cached_answer(make_state("pregunta"))).entry  # type: ignore[union-attr]

    assert entry_after.created_at == entry_before.created_at


# ------------------------------------------------------------- episodic learning


async def test_a_completed_task_leaves_an_episode() -> None:
    memory = _memory()
    deps = make_deps(gateway=make_gateway(FakeTransport(replies=["listo"])))
    deps.memory = memory
    app = build_graph(deps)

    await run(app, make_state("como solicito vacaciones"))

    hints = await memory.episodic.hints(
        "acme-mx", "solicito vacaciones", area="finanzas", ceiling=Classification.C2
    )
    assert hints


# --------------------------------------------------------------------- forget


async def test_forget_removes_what_the_graph_stored_and_a_test_can_prove_it() -> None:
    """F2 exit criterion: forget deletes, demonstrably."""
    memory = _memory()
    deps = make_deps(gateway=make_gateway(FakeTransport(replies=["respuesta"])))
    deps.memory = memory
    app = build_graph(deps)

    state = make_state("quiero saber del presupuesto del area")
    final = await run(app, state, thread="hilo")
    await memory.remember_facts(final, ["Ana pregunta por el presupuesto"])

    assert await memory.short_term.history("acme-mx", final.thread_id)
    assert await memory.long_term.recall("acme-mx", "presupuesto", area="finanzas", user_id="u1")
    assert await memory.cached_answer(state) is not None

    report = await memory.forget(
        "acme-mx", scope="tenant", area="finanzas", thread_ids=[final.thread_id]
    )

    assert report.total > 0
    assert await memory.short_term.history("acme-mx", final.thread_id) == []
    assert (
        await memory.long_term.recall("acme-mx", "presupuesto", area="finanzas", user_id="u1") == []
    )
    assert await memory.cached_answer(state) is None
    assert [k async for k in memory.store.scan("af:acme-mx:*")] == []


async def test_nothing_the_graph_persists_contains_raw_pii() -> None:
    memory = _memory()
    deps = make_deps(gateway=make_gateway(FakeTransport(replies=["Te escribo a ana@acme.com"])))
    deps.memory = memory
    app = build_graph(deps)

    await run(app, make_state("mi correo es ana@acme.com"), thread="hilo")

    dumped = "".join(
        [await memory.store.get(k) or "" async for k in memory.store.scan("af:acme-mx:*")]
    )
    assert "ana@acme.com" not in dumped, "raw PII reached a persistent store"
    assert "[EMAIL]" in dumped


async def test_memory_is_optional_and_the_graph_still_runs() -> None:
    """A cell with no memory configured must answer, just without continuity."""
    deps = make_deps(gateway=make_gateway(FakeTransport(replies=["ok"])))
    assert deps.memory is None
    app = build_graph(deps)

    final = await run(app, make_state("hola"))

    assert final.status == "completed"
    assert final.answer == "ok"


async def test_the_stored_turn_records_both_sides() -> None:
    memory = _memory()
    deps = make_deps(gateway=make_gateway(FakeTransport(replies=["respuesta del agente"])))
    deps.memory = memory
    app = build_graph(deps)

    final = await run(app, make_state("pregunta del usuario"), thread="hilo")

    history = await memory.short_term.history("acme-mx", final.thread_id)
    assert [m.role for m in history] == ["user", "assistant"]
    assert history[1] == Message(
        role="assistant", content="respuesta del agente", created_at=history[1].created_at
    )


async def test_a_question_containing_pii_is_not_cached_at_all() -> None:
    """Caching it redacted would degrade the replay; keying on the redaction would let a
    different person's question match the same entry. Skipping is the only safe option."""
    memory = _memory()
    deps = make_deps(gateway=make_gateway(FakeTransport(replies=["te respondo"])))
    deps.memory = memory
    app = build_graph(deps)
    state = make_state("mi correo es ana@acme.com, cual es mi saldo")

    await run(app, state, thread="hilo")

    assert await memory.cached_answer(state) is None


async def test_an_answer_containing_pii_is_not_cached_either() -> None:
    memory = _memory()
    deps = make_deps(gateway=make_gateway(FakeTransport(replies=["escribe a soporte@acme.com"])))
    deps.memory = memory
    app = build_graph(deps)
    state = make_state("a quien escribo para soporte")

    await run(app, state, thread="hilo")

    assert await memory.cached_answer(state) is None
