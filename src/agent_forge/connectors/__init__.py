"""Connectors: external systems as typed, governed tools.

The contract lives in `base`; `registry` mediates every call. Adding a connector is an
entry point and a `uv sync` -- the core is never edited.
"""

from __future__ import annotations

from agent_forge.connectors.base import (
    BaseConnector,
    CallContext,
    CircuitBreaker,
    ConnectorBase,
    ToolResult,
    ToolSpec,
    digest,
    guard,
)
from agent_forge.connectors.registry import (
    Allowlist,
    ConnectorError,
    ConnectorRegistry,
    PolicyHook,
    ToolInvocationRecord,
    build_registry,
    context_from_state,
)

__all__ = [
    "Allowlist",
    "BaseConnector",
    "CallContext",
    "CircuitBreaker",
    "ConnectorBase",
    "ConnectorError",
    "ConnectorRegistry",
    "PolicyHook",
    "ToolInvocationRecord",
    "ToolResult",
    "ToolSpec",
    "build_registry",
    "context_from_state",
    "digest",
    "guard",
]
