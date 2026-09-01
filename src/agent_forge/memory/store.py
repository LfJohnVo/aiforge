"""Key-value storage behind a `Protocol`.

Every memory layer writes through this. Two implementations: Redis for real deployments,
and an in-process one so the whole memory stack can be exercised in unit tests without
infrastructure -- which is also what makes the tenant-isolation tests cheap enough to run
on every commit.

Keys are always built with :func:`namespace`. The tenant is *part of the key*, not a
filter applied afterwards, so cross-tenant reads are impossible rather than merely
discouraged.
"""

from __future__ import annotations

import fnmatch
import json
import time
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from agent_forge.core.errors import AgentForgeError
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

KEY_PREFIX = "af"


class MemoryStoreError(AgentForgeError):
    """The memory store is unreachable or rejected an operation."""

    code = "memory_store_error"


def namespace(tenant_id: str, instance: str, *parts: str) -> str:
    """Build a namespaced key: ``af:{tenant}:{instance}:{parts...}``."""
    if not tenant_id:
        raise MemoryStoreError("tenant_id is required to build a memory key")
    clean = [p.replace(":", "_") for p in parts if p]
    return ":".join([KEY_PREFIX, tenant_id, instance or "default", *clean])


def tenant_pattern(tenant_id: str, instance: str = "*") -> str:
    """Glob matching everything belonging to one tenant. Used by ``forget``."""
    return f"{KEY_PREFIX}:{tenant_id}:{instance}:*"


@runtime_checkable
class KeyValueStore(Protocol):
    """Minimal surface the memory layers need."""

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None: ...

    async def delete(self, *keys: str) -> int: ...

    def scan(self, pattern: str) -> AsyncIterator[str]: ...

    async def health(self) -> bool: ...

    async def aclose(self) -> None: ...


class InMemoryStore:
    """Process-local store with real TTL semantics.

    Not a stub: expiry is honoured, which is what lets the STM TTL and the semantic cache
    TTL be tested without waiting on Redis.
    """

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float | None]] = {}

    def _expired(self, key: str) -> bool:
        entry = self._data.get(key)
        if entry is None:
            return True
        _, expires_at = entry
        if expires_at is not None and expires_at <= time.monotonic():
            self._data.pop(key, None)
            return True
        return False

    async def get(self, key: str) -> str | None:
        if self._expired(key):
            return None
        return self._data[key][0]

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        expires_at = time.monotonic() + ttl_seconds if ttl_seconds else None
        self._data[key] = (value, expires_at)

    async def delete(self, *keys: str) -> int:
        return sum(1 for key in keys if self._data.pop(key, None) is not None)

    async def scan(self, pattern: str) -> AsyncIterator[str]:
        for key in list(self._data):
            if not self._expired(key) and fnmatch.fnmatchcase(key, pattern):
                yield key

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        self._data.clear()


class RedisStore:
    """Redis-backed store. The production implementation."""

    def __init__(self, url: str, *, password: str = "") -> None:
        # Imported here so a cell running on the in-memory store never opens a pool.
        from redis.asyncio import Redis

        self._client: Any = Redis.from_url(
            url,
            password=password or None,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=10,
            health_check_interval=30,
        )

    async def get(self, key: str) -> str | None:
        try:
            value = await self._client.get(key)
        except Exception as exc:
            raise MemoryStoreError("redis GET failed", key=key, detail=str(exc)) from exc
        return str(value) if value is not None else None

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        try:
            await self._client.set(key, value, ex=ttl_seconds)
        except Exception as exc:
            raise MemoryStoreError("redis SET failed", key=key, detail=str(exc)) from exc

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        try:
            return int(await self._client.delete(*keys))
        except Exception as exc:
            raise MemoryStoreError("redis DEL failed", detail=str(exc)) from exc

    async def scan(self, pattern: str) -> AsyncIterator[str]:
        """SCAN, never KEYS: KEYS blocks the server, and forget() can match a lot."""
        try:
            async for key in self._client.scan_iter(match=pattern, count=500):
                yield str(key)
        except Exception as exc:
            raise MemoryStoreError("redis SCAN failed", pattern=pattern, detail=str(exc)) from exc

    async def health(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()


def build_store(redis_url: str = "", *, password: str = "") -> KeyValueStore:
    """Redis when a URL is configured, in-memory otherwise.

    Falling back is deliberate and loud: a cell must start on a laptop, but an operator
    has to know that memory will not survive a restart.
    """
    if redis_url:
        return RedisStore(redis_url, password=password)
    log.warning(
        "memory.in_memory_store_selected",
        detail="REDIS_URL is not set; memory is lost on restart and not shared between replicas",
    )
    return InMemoryStore()


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


def loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("memory.corrupt_entry_discarded")
        return default
