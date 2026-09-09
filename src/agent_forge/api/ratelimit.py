"""Per-credential request limiting.

This is defence in depth, not the primary control. LiteLLM already enforces a token
budget per tenant, which is what stops a *cost* runaway; this stops a *request* runaway,
which is a different failure: a loop in someone's integration can exhaust connections,
Postgres sessions and the model queue long before it exhausts a monthly budget, and the
tenant that suffers is usually not the one with the bug.

Three decisions worth naming, because each one is a corner cut on purpose:

**Per credential, not per user.** The limiter runs before routing, so no identity has
been resolved yet -- resolving one here would mean a second copy of the authentication
logic, and two copies of an auth path is how they drift. A credential maps to a tenant
for API keys, and to a person for OIDC, which is the granularity that matters either way.
Requests with no credential are keyed by client address; they are about to be rejected by
authentication anyway, and the point is only that rejecting them stays cheap.

**Fixed window, not a token bucket.** A caller can send the full quota at the end of one
minute and again at the start of the next, so the real worst case is twice the limit over
a two-minute boundary. For protecting a cell from a runaway loop that is fine, and a
sliding window costs a sorted set per caller.

**Fails open.** If the store is unreachable the request proceeds. A limiter that turns a
Redis blip into a total outage causes the harm it exists to prevent -- and unlike the
governance checks, nothing here protects data: the cost of being wrong is a busy cell,
not a leak.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass

from agent_forge.memory.store import KeyValueStore
from agent_forge.observability.logging import get_logger
from agent_forge.observability.metrics import get_metrics

log = get_logger(__name__)

WINDOW_SECONDS = 60
# Kept long enough that a key written at the very end of a window still expires well
# after that window closes, and short enough that a dead caller leaves nothing behind.
KEY_TTL_SECONDS = 2 * WINDOW_SECONDS

# Never limited. Docker probes /health/live every 15 seconds and Prometheus scrapes
# /metrics every 15: rate-limiting either turns a busy minute into a crash loop, which is
# the outage the limiter was installed to avoid.
EXEMPT_PREFIXES = ("/health", "/metrics")


@dataclass(frozen=True, slots=True)
class RateLimitVerdict:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


def credential_key(authorization: str | None, api_key: str | None, client: str) -> str:
    """A stable, non-reversible identifier for the caller.

    The credential is hashed rather than stored: these keys live in Redis, appear in
    `SCAN` output and in any dump of it, and a bearer token in a key name is a bearer
    token in a backup.
    """
    secret = (authorization or api_key or "").strip()
    if not secret:
        return f"addr:{client}"
    return "cred:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()[:32]


def build_rate_limiter(
    store: KeyValueStore, *, env: Mapping[str, str] | None = None, instance: str = "default"
) -> RateLimiter:
    """Read the quota from the environment. ``RATE_LIMIT_PER_MINUTE=0`` disables it.

    A default rather than off: a limit nobody enabled protects nobody, and 120 requests a
    minute for one credential is far above interactive use and far below what it takes to
    starve the model queue.
    """
    source = env if env is not None else os.environ
    try:
        per_minute = int(source.get("RATE_LIMIT_PER_MINUTE", "120"))
    except ValueError:
        log.error(
            "ratelimit.bad_configuration",
            value=source.get("RATE_LIMIT_PER_MINUTE"),
            detail="not an integer; falling back to the default of 120 per minute",
        )
        per_minute = 120
    return RateLimiter(store, per_minute=per_minute, instance=instance)


class RateLimiter:
    """Fixed-window counter over the shared key-value store.

    The store is shared on purpose: two replicas behind one ingress have to count
    together, or the effective limit is the limit times the replica count.
    """

    def __init__(
        self,
        store: KeyValueStore,
        *,
        per_minute: int,
        instance: str = "default",
    ) -> None:
        self._store = store
        self._per_minute = per_minute
        self._instance = instance

    @property
    def enabled(self) -> bool:
        return self._per_minute > 0

    async def check(self, key: str, *, now: float | None = None) -> RateLimitVerdict:
        if not self.enabled:
            return RateLimitVerdict(True, self._per_minute, self._per_minute, 0)

        moment = time.time() if now is None else now
        window = int(moment // WINDOW_SECONDS)
        try:
            used = await self._store.incr(
                f"ratelimit:{self._instance}:{key}:{window}", ttl_seconds=KEY_TTL_SECONDS
            )
        except Exception as exc:
            log.error(
                "ratelimit.store_unavailable",
                detail=str(exc),
                impact="requests proceed unlimited until the store recovers",
            )
            return RateLimitVerdict(True, self._per_minute, self._per_minute, 0)

        remaining = max(0, self._per_minute - used)
        if used <= self._per_minute:
            return RateLimitVerdict(True, self._per_minute, remaining, 0)

        # Seconds until this window closes, so a client that honours Retry-After comes
        # back exactly when it can be served rather than hammering.
        retry_after = max(1, int((window + 1) * WINDOW_SECONDS - moment))
        get_metrics().rate_limited.labels(instance=self._instance).inc()
        return RateLimitVerdict(False, self._per_minute, 0, retry_after)
