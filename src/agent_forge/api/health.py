"""Health endpoints.

``/health/live`` answers "is this process alive" and nothing else: it must not depend on
anything external, or a database blip restarts a perfectly healthy container.

``/health/ready`` answers "can this cell serve traffic", per dependency, plus which
optional capabilities are actually installed. The distinction matters in the runbook:
``postgres: down`` and ``knowledge: unavailable`` are different problems with different
responses.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from agent_forge.channels.openai_api import get_runtime
from agent_forge.observability.logging import get_logger
from agent_forge.runtime import Runtime

log = get_logger(__name__)

router = APIRouter(tags=["health"])

PROBE_TIMEOUT = 3.0


@router.get("/health/live")
async def live() -> dict[str, str]:
    """Liveness. No dependencies by design."""
    return {"status": "alive"}


@router.get("/health")
async def health(runtime: Annotated[Runtime, Depends(get_runtime)]) -> Any:
    """Alias for readiness, for clients that only know ``/health``."""
    return await ready(runtime)


@router.get("/health/ready")
async def ready(runtime: Annotated[Runtime, Depends(get_runtime)]) -> Any:
    """Readiness: one entry per dependency, plus installed capabilities."""
    checks = await _run_probes(runtime)
    healthy = all(checks.values())
    body = {
        "status": "ready" if healthy else "degraded",
        "instance": runtime.instance_key,
        "environment": runtime.settings.environment,
        "agent": runtime.profile.identity.agent_name,
        "area": runtime.profile.identity.area,
        "subgraph": runtime.deps.subgraph.name,
        "checkpointer": runtime.settings.checkpointer,
        "dependencies": {name: ("up" if ok else "down") for name, ok in checks.items()},
        "capabilities": {
            name: ("installed" if ok else "unavailable")
            for name, ok in sorted(runtime.capabilities.items())
        },
        "channels": runtime.profile.channels.enabled_names(),
    }
    return JSONResponse(body, status_code=200 if healthy else 503)


async def _run_probes(runtime: Runtime) -> dict[str, bool]:
    """Probe every dependency concurrently, with a timeout each.

    A hanging dependency must not make readiness itself hang: that turns a degraded cell
    into an unresponsive one.
    """
    probes: dict[str, Callable[[], Awaitable[bool]]] = {
        "model_gateway": runtime.gateway.health,
    }
    results = await asyncio.gather(
        *(_probe(name, fn) for name, fn in probes.items()), return_exceptions=False
    )
    return dict(results)


async def _probe(name: str, fn: Callable[[], Awaitable[bool]]) -> tuple[str, bool]:
    try:
        return name, bool(await asyncio.wait_for(fn(), timeout=PROBE_TIMEOUT))
    except (TimeoutError, Exception) as exc:
        log.warning("health.probe_failed", dependency=name, detail=type(exc).__name__)
        return name, False
