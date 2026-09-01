"""Checkpointer selection.

Postgres is the principal store: a task that pauses for human approval may wait days, and
it has to survive every restart in between. Redis is available for ephemeral state where
that durability is not required, and an in-memory saver exists so unit tests can exercise
the real resume path without infrastructure.

Thread ids are namespaced by tenant and instance. Two cells on the same host share
nothing, and a `thread_id` collision between tenants is not possible by construction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from agent_forge.core.errors import AgentForgeError
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

CheckpointerKind = Literal["postgres", "redis", "memory"]


class CheckpointerError(AgentForgeError):
    """The configured checkpointer could not be created."""

    code = "checkpointer_unavailable"


def namespaced_thread_id(tenant_id: str, instance: str, thread_id: str) -> str:
    """``tenant:instance:thread``.

    The namespace is part of the key, not a filter applied afterwards: that is what makes
    tenant isolation a property of the store rather than of every call site.
    """
    if not tenant_id:
        raise CheckpointerError("tenant_id is required to build a thread id")
    return f"{tenant_id}:{instance or 'default'}:{thread_id}"


@asynccontextmanager
async def open_checkpointer(
    kind: CheckpointerKind,
    *,
    postgres_dsn: str = "",
    redis_url: str = "",
) -> AsyncIterator[BaseCheckpointSaver[str]]:
    """Yield a checkpointer, setting up its schema on first use.

    Imports are local so that a cell running with the memory saver never needs the
    Postgres or Redis checkpoint packages loaded.
    """
    if kind == "memory":
        log.warning(
            "checkpointer.memory_selected",
            detail="state is lost on restart; not for production",
        )
        yield InMemorySaver()
        return

    if kind == "postgres":
        if not postgres_dsn:
            raise CheckpointerError("POSTGRES_DSN is required for the postgres checkpointer")
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(postgres_dsn) as saver:
            await saver.setup()
            log.info("checkpointer.ready", kind="postgres")
            yield saver
        return

    if kind == "redis":
        if not redis_url:
            raise CheckpointerError("REDIS_URL is required for the redis checkpointer")
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver

        async with AsyncRedisSaver.from_conn_string(redis_url) as saver:
            await saver.asetup()
            log.info("checkpointer.ready", kind="redis")
            yield saver
        return

    raise CheckpointerError("unknown checkpointer kind", kind=kind)
