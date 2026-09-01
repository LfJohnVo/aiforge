"""The memory contract against a real Redis.

The unit suite runs everything against ``InMemoryStore``. This runs the parts that can
diverge -- TTL, SCAN, delete counts -- against the store that actually ships, because a
key-space bug that only appears on Redis is a bug nobody sees until production.

Requires Docker. Skipped automatically when it is not available.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_forge.core.state import Message
from agent_forge.memory import (
    KeyValueLongTermMemory,
    MemoryFact,
    RedisStore,
    Scrubber,
    SemanticCache,
    ShortTermMemory,
    build_memory,
    namespace,
)

pytestmark = pytest.mark.integration

TENANT_A = "acme-mx"
TENANT_B = "otra-empresa"


@pytest.fixture(scope="module")
def redis_url() -> Any:
    testcontainers = pytest.importorskip("testcontainers.redis")
    try:
        container = testcontainers.RedisContainer("redis:8.8-alpine")
        container.start()
    except Exception as exc:  # no docker here means skip, not fail
        pytest.skip(f"docker unavailable for integration tests: {exc}")
    try:
        yield f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"
    finally:
        container.stop()


@pytest.fixture
async def store(redis_url: str) -> AsyncIterator[RedisStore]:
    backing = RedisStore(redis_url)
    # Each test starts from a clean key space, or ordering changes the results.
    keys = [key async for key in backing.scan("af:*")]
    if keys:
        await backing.delete(*keys)
    yield backing
    await backing.aclose()


async def test_round_trip_and_delete(store: RedisStore) -> None:
    await store.set("af:t:i:probe", "value")

    assert await store.get("af:t:i:probe") == "value"
    assert await store.delete("af:t:i:probe") == 1
    assert await store.get("af:t:i:probe") is None


async def test_ttl_is_applied(store: RedisStore) -> None:
    await store.set("af:t:i:ttl", "value", ttl_seconds=60)

    # Reading the TTL back proves the expiry reached the server, without waiting for it.
    ttl = await store._client.ttl("af:t:i:ttl")
    assert 0 < ttl <= 60


async def test_scan_is_scoped_to_the_pattern(store: RedisStore) -> None:
    await store.set(namespace(TENANT_A, "i", "stm", "1"), "a")
    await store.set(namespace(TENANT_A, "i", "ltm", "area:finanzas"), "b")
    await store.set(namespace(TENANT_B, "i", "stm", "1"), "c")

    found = sorted([k async for k in store.scan(f"af:{TENANT_A}:i:stm:*")])

    assert found == [f"af:{TENANT_A}:i:stm:1"]


async def test_health_is_true_against_a_live_server(store: RedisStore) -> None:
    assert await store.health() is True


async def test_short_term_round_trips_through_redis(store: RedisStore) -> None:
    stm = ShortTermMemory(store, instance="itest", scrubber=Scrubber(use_presidio=False))

    await stm.append(TENANT_A, "hilo", Message(role="user", content="me llamo Ana"))
    await stm.append(TENANT_A, "hilo", Message(role="assistant", content="hola Ana"))

    history = await stm.history(TENANT_A, "hilo")
    assert [m.content for m in history] == ["me llamo Ana", "hola Ana"]


async def test_tenants_stay_isolated_on_redis(store: RedisStore) -> None:
    ltm = KeyValueLongTermMemory(store, instance="itest", scope="area")
    await ltm.remember(
        TENANT_A, [MemoryFact(text="dato privado de A", subject="u1")], area="finanzas"
    )

    assert await ltm.recall(TENANT_B, "dato privado", area="finanzas", user_id="u1") == []
    assert await ltm.recall(TENANT_A, "dato privado", area="finanzas", user_id="u1")


async def test_cache_reach_check_holds_on_redis(store: RedisStore) -> None:
    from agent_forge.core.classification import Classification

    cache = SemanticCache(store, instance="itest", similarity=0.9)
    await cache.put(
        TENANT_A,
        "resumen de nomina",
        "sueldos",
        area="finanzas",
        classification=Classification.C2,
        groups=["finanzas", "finanzas-lideres"],
    )

    denied = await cache.get(
        TENANT_A,
        "resumen de nomina",
        area="finanzas",
        ceiling=Classification.C2,
        groups=["finanzas"],
    )
    allowed = await cache.get(
        TENANT_A,
        "resumen de nomina",
        area="finanzas",
        ceiling=Classification.C2,
        groups=["finanzas", "finanzas-lideres"],
    )

    assert denied is None
    assert allowed is not None


async def test_forget_tenant_leaves_no_key_behind_on_redis(store: RedisStore) -> None:
    """The compliance path, on the store that actually ships."""
    memory = build_memory(store=store, instance="itest", scope="area")
    from tests.support import make_state

    state = make_state("pregunta con historia").model_copy(
        update={"status": "completed", "answer": "respuesta"}
    )
    await memory.record_turn(
        state, user_message=Message(role="user", content="pregunta con historia")
    )
    await memory.remember_facts(state, ["un hecho duradero"])
    await memory.record_outcome(state)
    assert [k async for k in store.scan(f"af:{TENANT_A}:itest:*")]

    report = await memory.forget(TENANT_A, scope="tenant", area="finanzas")

    assert report.total > 0
    assert [k async for k in store.scan(f"af:{TENANT_A}:itest:*")] == []
