"""Request limiting: who gets refused, who never does, and what happens when it breaks.

The interesting cases are the two exemptions. A limiter that refuses `/health/live`
turns a busy minute into a Docker restart loop, and a limiter that refuses to decide
when Redis blinks turns a blip into an outage -- both of which are worse than the
flooding the limiter exists to stop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from agent_forge.api.ratelimit import RateLimiter, build_rate_limiter, credential_key
from agent_forge.memory.store import InMemoryStore
from agent_forge.runtime import Runtime
from tests.cell import API_KEY, cell_env, make_app, make_runtime
from tests.support import FakeTransport

pytestmark = pytest.mark.anyio

AUTH = {"authorization": f"Bearer {API_KEY}"}


@pytest.fixture
def runtime() -> Runtime:
    return make_runtime(FakeTransport(replies=["hola"]))


@pytest.fixture
def app(runtime: Runtime) -> Iterator[FastAPI]:
    """Three requests a minute, so a test does not have to send a hundred and twenty."""
    yield make_app(runtime, env=cell_env(RATE_LIMIT_PER_MINUTE="3"))


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://cell") as http,
    ):
        yield http


# ------------------------------------------------------------------- the counter


async def test_the_quota_is_per_credential_so_one_caller_cannot_starve_another() -> None:
    limiter = RateLimiter(InMemoryStore(), per_minute=2)

    for _ in range(2):
        assert (await limiter.check("cred:a")).allowed
    assert not (await limiter.check("cred:a")).allowed
    assert (await limiter.check("cred:b")).allowed, "b pays for a's flood"


async def test_the_window_rolls_over() -> None:
    limiter = RateLimiter(InMemoryStore(), per_minute=1)

    assert (await limiter.check("cred:a", now=100.0)).allowed
    assert not (await limiter.check("cred:a", now=110.0)).allowed
    assert (await limiter.check("cred:a", now=160.0)).allowed


async def test_retry_after_points_at_the_end_of_the_window_not_a_fixed_delay() -> None:
    """A client that honours the header should come back exactly when it can be served."""
    limiter = RateLimiter(InMemoryStore(), per_minute=1)
    await limiter.check("cred:a", now=100.0)

    verdict = await limiter.check("cred:a", now=100.0)

    assert verdict.retry_after == 20  # the window covering t=100 closes at t=120


async def test_a_zero_quota_disables_the_limiter_entirely() -> None:
    limiter = build_rate_limiter(InMemoryStore(), env={"RATE_LIMIT_PER_MINUTE": "0"})

    assert not limiter.enabled
    assert (await limiter.check("cred:a")).allowed


async def test_a_broken_quota_setting_falls_back_rather_than_refusing_to_start() -> None:
    limiter = build_rate_limiter(InMemoryStore(), env={"RATE_LIMIT_PER_MINUTE": "muchas"})

    assert limiter.enabled


async def test_an_unreachable_store_lets_the_request_through() -> None:
    """Fail open: nothing here protects data, so the cost of being wrong is a busy cell."""

    class Broken(InMemoryStore):
        async def incr(self, key: str, *, ttl_seconds: int) -> int:
            raise RuntimeError("redis is gone")

    limiter = RateLimiter(Broken(), per_minute=1)

    assert (await limiter.check("cred:a")).allowed
    assert (await limiter.check("cred:a")).allowed


# ------------------------------------------------------------------- the key


def test_the_credential_never_appears_in_the_key() -> None:
    """These keys land in Redis, in SCAN output and in every backup of it."""
    key = credential_key("Bearer super-secret-token", None, "10.0.0.1")

    assert "super-secret-token" not in key
    assert key.startswith("cred:")


def test_an_anonymous_caller_is_keyed_by_address() -> None:
    assert credential_key(None, None, "10.0.0.1") == "addr:10.0.0.1"


def test_two_headers_carrying_the_same_token_share_one_quota() -> None:
    """Otherwise moving the token from one header to the other doubles the allowance."""
    assert credential_key("tok", None, "ip") == credential_key(None, "tok", "ip")


# ------------------------------------------------------------------- the wiring


async def test_a_flooding_caller_gets_429_with_a_retry_after_header(
    client: httpx.AsyncClient,
) -> None:
    seen = [(await client.get("/v1/models", headers=AUTH)).status_code for _ in range(4)]

    assert seen[:3] == [200, 200, 200]
    assert seen[3] == 429

    refused = await client.get("/v1/models", headers=AUTH)
    assert refused.headers["Retry-After"].isdigit()
    assert refused.json()["code"] == "rate_limited"


async def test_an_allowed_request_reports_what_is_left(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models", headers=AUTH)

    assert response.headers["X-RateLimit-Limit"] == "3"
    assert response.headers["X-RateLimit-Remaining"] == "2"


async def test_the_liveness_probe_is_never_limited(client: httpx.AsyncClient) -> None:
    """Docker probes this every 15s. Limiting it restarts a container that is healthy."""
    for _ in range(10):
        assert (await client.get("/health/live")).status_code == 200


async def test_scraping_metrics_is_never_limited(client: httpx.AsyncClient) -> None:
    for _ in range(10):
        assert (await client.get("/metrics")).status_code == 200


async def test_an_unconfigured_app_limits_nothing(runtime: Runtime) -> None:
    """Before the lifespan builds it there is no limiter, and requests must still flow."""
    app: Any = make_app(runtime)
    app.state.rate_limiter = None
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://cell") as http,
    ):
        app.state.rate_limiter = None
        for _ in range(5):
            assert (await http.get("/health/live")).status_code == 200
