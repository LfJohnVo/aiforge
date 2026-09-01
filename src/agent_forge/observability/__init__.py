"""Observability: logs, traces, metrics and LLM traces.

One entry point, ``setup_observability``, called once at startup. The three backends are
independent -- a cell can have metrics and no collector, or logs and nothing else -- and
each one degrades to a no-op rather than to an error, so the same instrumentation code
runs on a laptop and in a cluster.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_forge.observability.langfuse_client import LangfuseTracer, build_langfuse
from agent_forge.observability.logging import (
    bind_request_context,
    clear_request_context,
    configure_logging,
    get_logger,
)
from agent_forge.observability.metrics import Metrics, get_metrics, render, reset_metrics
from agent_forge.observability.tracing import (
    current_trace_id,
    setup_tracing,
    shutdown_tracing,
    span,
    span_for_state,
)

__all__ = [
    "LangfuseTracer",
    "Metrics",
    "Observability",
    "bind_request_context",
    "build_langfuse",
    "clear_request_context",
    "configure_logging",
    "current_trace_id",
    "get_logger",
    "get_metrics",
    "render",
    "reset_metrics",
    "setup_observability",
    "setup_tracing",
    "shutdown_tracing",
    "span",
    "span_for_state",
]


@dataclass(slots=True)
class Observability:
    """What the runtime holds on to."""

    metrics: Metrics
    langfuse: LangfuseTracer
    tracing: bool = False

    async def aclose(self) -> None:
        self.langfuse.flush()
        self.langfuse.shutdown()
        shutdown_tracing()


def setup_observability(
    *,
    settings: Any,
    profile: Any,
    env: dict[str, str],
) -> Observability:
    """Wire tracing, metrics and Langfuse from the profile and the environment."""
    tracer = setup_tracing(
        endpoint=profile.observability.otel_endpoint,
        service=profile.identity.agent_name or "agent-forge",
        instance=settings.instance,
        environment=settings.environment,
    )
    langfuse = build_langfuse(
        public_key=env.get("LANGFUSE_PUBLIC_KEY", ""),
        secret_key=env.get("LANGFUSE_SECRET_KEY", ""),
        host=env.get("LANGFUSE_HOST", ""),
        environment=settings.environment,
        enabled=profile.observability.langfuse.enabled,
    )
    return Observability(metrics=get_metrics(), langfuse=langfuse, tracing=tracer is not None)
