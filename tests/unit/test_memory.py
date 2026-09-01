"""Memory: scrubbing, short term, long term, episodic and the semantic cache.

The three that are not about convenience:

* tenant isolation is a property of the key, not of a filter,
* nothing persists un-scrubbed,
* ``forget`` reaches every layer.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from agent_forge.core.classification import Classification
from agent_forge.core.state import Message
from agent_forge.memory import (
    EpisodicMemory,
    InMemoryStore,
    KeyValueLongTermMemory,
    MemoryFact,
    Scrubber,
    SemanticCache,
    ShortTermMemory,
    build_memory,
    build_store,
    namespace,
)
from agent_forge.memory.store import MemoryStoreError
from tests.support import make_state

TENANT_A = "acme-mx"
TENANT_B = "otra-empresa"


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


# ---------------------------------------------------------------------- store


def test_tenant_is_part_of_the_key() -> None:
    assert namespace("acme", "inst", "stm", "t1") == "af:acme:inst:stm:t1"
    assert namespace("acme", "", "ltm") == "af:acme:default:ltm"


def test_a_key_without_a_tenant_is_refused() -> None:
    with pytest.raises(MemoryStoreError):
        namespace("", "inst", "stm")


async def test_in_memory_store_honours_ttl(store: InMemoryStore) -> None:
    await store.set("k", "v", ttl_seconds=1)
    assert await store.get("k") == "v"

    # Move the clock rather than sleeping: the test must stay fast.
    store._data["k"] = ("v", time.monotonic() - 1)

    assert await store.get("k") is None


async def test_scan_matches_only_the_pattern(store: InMemoryStore) -> None:
    await store.set("af:a:i:stm:1", "x")
    await store.set("af:a:i:ltm:area", "y")
    await store.set("af:b:i:stm:1", "z")

    found = [k async for k in store.scan("af:a:i:stm:*")]

    assert found == ["af:a:i:stm:1"]


def test_build_store_falls_back_loudly_without_redis() -> None:
    assert isinstance(build_store(""), InMemoryStore)


# ------------------------------------------------------------------- scrubbing


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("contacta a ana@acme.com", "EMAIL"),
        ("paga con 4111 1111 1111 1111", "CREDIT_CARD"),
        ("cuenta ES9121000418450200051332", "IBAN"),
        ("su RFC es VECJ880326XXX", "RFC"),
        ("CURP VECJ880326HDFRRN09", "CURP"),
        ("DNI 12345678Z", "NIF"),
        ("llama al +52 55 1234 5678", "PHONE"),
        ("host 10.0.0.5", "IP"),
        ("ssn 123-45-6789", "SSN"),
    ],
)
def test_pii_is_detected(text: str, kind: str) -> None:
    result = Scrubber(use_presidio=False).scrub(text)

    assert kind in result.kinds()
    assert f"[{kind}]" in result.text


@pytest.mark.parametrize(
    "text",
    [
        "el limite de viaticos es 1500 pesos",
        "pedido 1234567890123456 sin checksum valido",
        "la reunion es el 2024-09-01",
        "ticket INC-2024-0093 abierto",
        "DNI 99999999X con letra incorrecta",
    ],
)
def test_ordinary_text_is_left_alone(text: str) -> None:
    """False positives are not harmless: scrubbing an order id destroys the answer."""
    assert Scrubber(use_presidio=False).scrub(text).clean


def test_allowlisted_kinds_are_kept() -> None:
    """A SOC area legitimately needs to keep IP addresses."""
    scrubber = Scrubber(use_presidio=False, allow_kinds=frozenset({"IP"}))

    assert scrubber.scrub("host 10.0.0.5 comprometido").clean


def test_overlapping_matches_keep_the_longest() -> None:
    result = Scrubber(use_presidio=False).scrub("CURP VECJ880326HDFRRN09 fin")

    assert result.kinds() == ["CURP"]
    assert result.text == "CURP [CURP] fin"


def test_a_disabled_scrubber_is_a_passthrough() -> None:
    assert Scrubber(enabled=False).scrub("ana@acme.com").text == "ana@acme.com"


# ------------------------------------------------------------------ short term


async def test_session_survives_between_calls(store: InMemoryStore) -> None:
    stm = ShortTermMemory(store, ttl_minutes=60)

    await stm.append(TENANT_A, "t1", Message(role="user", content="me llamo Ana"))
    await stm.append(TENANT_A, "t1", Message(role="assistant", content="hola"))

    history = await stm.history(TENANT_A, "t1")
    assert [m.content for m in history] == ["me llamo Ana", "hola"]


async def test_short_term_is_isolated_by_tenant(store: InMemoryStore) -> None:
    stm = ShortTermMemory(store)

    await stm.append(TENANT_A, "same-thread", Message(role="user", content="secreto de A"))
    await stm.append(TENANT_B, "same-thread", Message(role="user", content="secreto de B"))

    a = await stm.history(TENANT_A, "same-thread")
    b = await stm.history(TENANT_B, "same-thread")

    assert [m.content for m in a] == ["secreto de A"]
    assert [m.content for m in b] == ["secreto de B"]


async def test_messages_are_scrubbed_before_they_are_stored(store: InMemoryStore) -> None:
    stm = ShortTermMemory(store, scrubber=Scrubber(use_presidio=False))

    await stm.append(TENANT_A, "t1", Message(role="user", content="mi correo es ana@acme.com"))

    raw = await store.get(namespace(TENANT_A, "default", "stm", "t1"))
    assert raw is not None
    assert "ana@acme.com" not in raw
    assert "[EMAIL]" in raw


async def test_overflow_condenses_instead_of_truncating(store: InMemoryStore) -> None:
    """The oldest turns must survive as a summary, not vanish."""
    stm = ShortTermMemory(store, window=4)

    for i in range(10):
        await stm.append(TENANT_A, "t1", Message(role="user", content=f"dato-{i}"))

    session = await stm.load(TENANT_A, "t1")
    assert len(session.messages) <= 6
    assert session.turns_summarised > 0
    assert "dato-0" in session.summary, "the earliest turn must survive in the summary"

    context = await stm.history(TENANT_A, "t1")
    assert context[0].role == "system"
    assert "Resumen" in context[0].content


async def test_clear_and_forget_tenant(store: InMemoryStore) -> None:
    stm = ShortTermMemory(store)
    await stm.append(TENANT_A, "t1", Message(role="user", content="x"))
    await stm.append(TENANT_A, "t2", Message(role="user", content="y"))
    await stm.append(TENANT_B, "t1", Message(role="user", content="z"))

    assert await stm.forget_tenant(TENANT_A) == 2
    assert await stm.history(TENANT_A, "t1") == []
    assert len(await stm.history(TENANT_B, "t1")) == 1


# -------------------------------------------------------------- long term (contract)


def _adapters(store: InMemoryStore) -> dict[str, Any]:
    """Every registered LTM adapter that can run without extras (ADR-002)."""
    return {
        "keyvalue-area": KeyValueLongTermMemory(
            store, scope="area", scrubber=Scrubber(use_presidio=False)
        ),
        "keyvalue-user": KeyValueLongTermMemory(
            store, scope="user", scrubber=Scrubber(use_presidio=False)
        ),
    }


@pytest.fixture(params=["keyvalue-area", "keyvalue-user"])
def ltm(request: pytest.FixtureRequest, store: InMemoryStore) -> Any:
    return _adapters(store)[request.param]


async def test_contract_remember_then_recall(ltm: Any) -> None:
    await ltm.remember(
        TENANT_A,
        [MemoryFact(text="El limite de viaticos es 1500 MXN", subject="u1")],
        area="finanzas",
    )

    found = await ltm.recall(
        TENANT_A, "cual es el limite de viaticos", area="finanzas", user_id="u1"
    )

    assert [f.text for f in found] == ["El limite de viaticos es 1500 MXN"]


async def test_contract_remembering_twice_is_idempotent(ltm: Any) -> None:
    fact = MemoryFact(text="Ana prefiere respuestas breves", subject="u1")

    first = await ltm.remember(TENANT_A, [fact], area="finanzas")
    second = await ltm.remember(TENANT_A, [fact], area="finanzas")

    assert (first, second) == (1, 0)


async def test_contract_tenants_are_isolated(ltm: Any) -> None:
    await ltm.remember(
        TENANT_A, [MemoryFact(text="secreto viaticos de A", subject="u1")], area="finanzas"
    )

    leaked = await ltm.recall(TENANT_B, "viaticos", area="finanzas", user_id="u1")

    assert leaked == []


async def test_contract_facts_are_scrubbed_before_storage(ltm: Any, store: InMemoryStore) -> None:
    await ltm.remember(
        TENANT_A, [MemoryFact(text="Ana usa ana@acme.com", subject="u1")], area="finanzas"
    )

    dump = "".join([await store.get(k) or "" async for k in store.scan(f"af:{TENANT_A}:*")])
    assert "ana@acme.com" not in dump
    assert "[EMAIL]" in dump


async def test_contract_forget_user_removes_their_facts(ltm: Any) -> None:
    await ltm.remember(TENANT_A, [MemoryFact(text="dato de ana", subject="ana")], area="finanzas")
    await ltm.remember(TENANT_A, [MemoryFact(text="dato de beto", subject="beto")], area="finanzas")

    removed = await ltm.forget(TENANT_A, user_id="ana", area="finanzas")

    assert removed == 1
    remaining = await ltm.recall(TENANT_A, "dato", area="finanzas", user_id="beto")
    assert all(f.subject != "ana" for f in remaining)


async def test_contract_forget_tenant_removes_everything(ltm: Any) -> None:
    await ltm.remember(TENANT_A, [MemoryFact(text="a", subject="u1")], area="finanzas")
    await ltm.remember(TENANT_A, [MemoryFact(text="b", subject="u2")], area="finanzas")

    await ltm.forget(TENANT_A, user_id=None, area="finanzas")

    assert await ltm.recall(TENANT_A, "a", area="finanzas", user_id="u1") == []
    assert await ltm.recall(TENANT_A, "b", area="finanzas", user_id="u2") == []


async def test_contract_health(ltm: Any) -> None:
    assert await ltm.health() is True


async def test_user_scope_does_not_read_the_shared_area_bucket(store: InMemoryStore) -> None:
    """A colleague's fact is area memory, not this user's memory."""
    area_scoped = KeyValueLongTermMemory(store, scope="area")
    user_scoped = KeyValueLongTermMemory(store, scope="user")
    await area_scoped.remember(
        TENANT_A, [MemoryFact(text="dato compartido del area", subject="otro")], area="finanzas"
    )

    found = await user_scoped.recall(TENANT_A, "dato compartido", area="finanzas", user_id="ana")

    assert found == []


async def test_expired_facts_are_not_recalled(store: InMemoryStore) -> None:
    ltm = KeyValueLongTermMemory(store, retention_days=1)
    old = MemoryFact(text="dato viejo", subject="u1", created_at=time.time() - 86400 * 2)
    await ltm.remember(TENANT_A, [old], area="finanzas")

    assert await ltm.recall(TENANT_A, "dato viejo", area="finanzas", user_id="u1") == []


# --------------------------------------------------------------- semantic cache


async def test_cache_returns_a_previous_answer(store: InMemoryStore) -> None:
    cache = SemanticCache(store, similarity=0.9)
    await cache.put(
        TENANT_A,
        "cual es el limite de viaticos",
        "1500 MXN",
        area="finanzas",
        classification=Classification.C2,
        groups=["finanzas"],
    )

    hit = await cache.get(
        TENANT_A,
        "cual es el limite de viaticos",
        area="finanzas",
        ceiling=Classification.C2,
        groups=["finanzas"],
    )

    assert hit is not None
    assert hit.entry.answer == "1500 MXN"
    assert cache.hits == 1
    assert cache.hit_rate == 1.0


async def test_cache_ignores_accents_and_case(store: InMemoryStore) -> None:
    cache = SemanticCache(store, similarity=0.9)
    await cache.put(TENANT_A, "¿Cuál es el límite?", "1500", area="f", groups=["g"])

    hit = await cache.get(TENANT_A, "cual es el limite", area="f", groups=["g"])

    assert hit is not None


async def test_cache_never_serves_above_the_requester_ceiling(store: InMemoryStore) -> None:
    cache = SemanticCache(store, similarity=0.9)
    await cache.put(
        TENANT_A,
        "dato restringido",
        "contenido C3",
        area="finanzas",
        classification=Classification.C3,
        groups=["finanzas"],
    )

    hit = await cache.get(
        TENANT_A,
        "dato restringido",
        area="finanzas",
        ceiling=Classification.C1,
        groups=["finanzas"],
    )

    assert hit is None, "a C3 answer must never be served to a C1 requester"


async def test_cache_never_serves_across_group_boundaries(store: InMemoryStore) -> None:
    """The subtle leak: same classification, different reach."""
    cache = SemanticCache(store, similarity=0.9)
    await cache.put(
        TENANT_A,
        "resumen de nomina",
        "sueldos del equipo",
        area="finanzas",
        classification=Classification.C2,
        groups=["finanzas", "finanzas-lideres"],
    )

    hit = await cache.get(
        TENANT_A,
        "resumen de nomina",
        area="finanzas",
        ceiling=Classification.C2,
        groups=["finanzas"],
    )

    assert hit is None


async def test_cache_is_isolated_by_tenant(store: InMemoryStore) -> None:
    cache = SemanticCache(store, similarity=0.9)
    await cache.put(TENANT_A, "pregunta", "respuesta de A", area="finanzas", groups=[])

    assert await cache.get(TENANT_B, "pregunta", area="finanzas", groups=[]) is None


async def test_a_different_question_misses(store: InMemoryStore) -> None:
    cache = SemanticCache(store, similarity=0.92)
    await cache.put(TENANT_A, "cual es el limite de viaticos", "1500", area="f", groups=[])

    hit = await cache.get(TENANT_A, "como solicito vacaciones", area="f", groups=[])

    assert hit is None
    assert cache.misses == 1


async def test_a_disabled_cache_never_hits(store: InMemoryStore) -> None:
    cache = SemanticCache(store, enabled=False)
    await cache.put(TENANT_A, "q", "a", area="f", groups=[])

    assert await cache.get(TENANT_A, "q", area="f", groups=[]) is None


async def test_cache_uses_embeddings_when_available(store: InMemoryStore) -> None:
    """With an embedder, paraphrases hit; the lexical fallback would miss them."""

    class StubEmbedder:
        async def embed(self, text: str) -> list[float]:
            # "viaticos" and "gastos de viaje" map to the same direction.
            travel = 1.0 if ("viatico" in text or "viaje" in text) else 0.0
            return [travel, 1.0 - travel]

    cache = SemanticCache(store, similarity=0.95, embedder=StubEmbedder())
    await cache.put(TENANT_A, "limite de viaticos", "1500", area="f", groups=[])

    hit = await cache.get(TENANT_A, "tope de gastos de viaje", area="f", groups=[])

    assert hit is not None
    assert hit.similarity == pytest.approx(1.0)


# ------------------------------------------------------------------- episodic


async def test_only_completed_tasks_become_episodes(store: InMemoryStore) -> None:
    episodic = EpisodicMemory(store)
    state = make_state("como pido vacaciones")

    assert await episodic.record(state) is None  # still running

    done = state.model_copy(update={"status": "completed"})
    assert await episodic.record(done) is not None


async def test_episodes_generalise_case_identifiers(store: InMemoryStore) -> None:
    episodic = EpisodicMemory(store)
    state = make_state("estado del ticket INC-2024-0093").model_copy(update={"status": "completed"})

    episode = await episodic.record(state)

    assert episode is not None
    assert "INC-2024-0093" not in episode.question_pattern
    assert "<id>" in episode.question_pattern


async def test_episodes_are_recalled_as_hints(store: InMemoryStore) -> None:
    episodic = EpisodicMemory(store)
    state = make_state("como solicito vacaciones").model_copy(update={"status": "completed"})
    await episodic.record(state)

    hints = await episodic.hints(
        TENANT_A, "solicito vacaciones", area="finanzas", ceiling=Classification.C2
    )

    assert hints and "vacaciones" in hints[0]


async def test_episodes_above_the_ceiling_are_not_recalled(store: InMemoryStore) -> None:
    episodic = EpisodicMemory(store)
    state = make_state("consulta restringida").model_copy(
        update={"status": "completed", "classification": Classification.C3}
    )
    await episodic.record(state)

    assert (
        await episodic.recall(
            TENANT_A, "consulta restringida", area="finanzas", ceiling=Classification.C1
        )
        == []
    )


# --------------------------------------------------------------------- forget


async def test_forget_user_reaches_every_layer() -> None:
    memory = build_memory(store=InMemoryStore(), scope="area")
    state = make_state("me llamo Ana y trabajo en finanzas").model_copy(
        update={"status": "completed", "answer": "Hola Ana."}
    )
    await memory.record_turn(
        state,
        user_message=Message(role="user", content="me llamo Ana"),
        assistant_message=Message(role="assistant", content="Hola Ana."),
    )
    await memory.remember_facts(state, ["Ana trabaja en finanzas"])
    await memory.record_outcome(state)

    assert await memory.long_term.recall(TENANT_A, "Ana", area="finanzas", user_id="u1")

    report = await memory.forget(
        TENANT_A,
        scope="user",
        user_id="u1",
        area="finanzas",
        thread_ids=[state.thread_id],
    )

    assert report.short_term == 1
    assert report.long_term == 1
    assert report.semantic_cache == 1
    assert await memory.long_term.recall(TENANT_A, "Ana", area="finanzas", user_id="u1") == []
    assert await memory.short_term.history(TENANT_A, state.thread_id) == []
    assert await memory.cached_answer(state) is None


async def test_forget_tenant_wipes_all_four_layers() -> None:
    memory = build_memory(store=InMemoryStore(), scope="area")
    state = make_state("pregunta").model_copy(update={"status": "completed", "answer": "respuesta"})
    await memory.record_turn(state, user_message=Message(role="user", content="pregunta"))
    await memory.remember_facts(state, ["un hecho"])
    await memory.record_outcome(state)

    report = await memory.forget(TENANT_A, scope="tenant", area="finanzas")

    assert report.total > 0
    assert report.short_term >= 1
    assert report.episodic >= 1
    remaining = [k async for k in memory.store.scan(f"af:{TENANT_A}:*")]
    assert remaining == []


async def test_forget_does_not_touch_another_tenant() -> None:
    memory = build_memory(store=InMemoryStore(), scope="area")
    other = make_state("pregunta de B", tenant_id=TENANT_B).model_copy(
        update={"status": "completed", "answer": "respuesta B"}
    )
    await memory.record_turn(other, user_message=Message(role="user", content="pregunta de B"))

    await memory.forget(TENANT_A, scope="tenant", area="finanzas")

    assert await memory.short_term.history(TENANT_B, other.thread_id)


async def test_stats_report_cache_effectiveness() -> None:
    memory = build_memory(store=InMemoryStore(), cache_similarity=0.9)
    state = make_state("pregunta repetida").model_copy(
        update={"status": "completed", "answer": "respuesta"}
    )

    assert await memory.cached_answer(state) is None  # miss
    await memory.record_outcome(state)
    assert await memory.cached_answer(state) is not None  # hit

    stats = memory.stats()
    assert stats["cache_hits"] == 1
    assert stats["cache_misses"] == 1
    assert stats["cache_hit_rate"] == 0.5
