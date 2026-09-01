"""MCP client: the cell as a consumer of the platform's Tool Fabric.

The cell **does not implement an MCP gateway** (anti-goal 2). It speaks the protocol to
whatever gateway the platform runs -- Docker MCP Gateway, IBM ContextForge, MCPX, or a
bespoke one -- and re-exposes the remote tools as its own, prefixed so a remote name can
never collide with a local one.

Two things the re-exposure has to get right, because the remote server has no way to know
them:

* **Autonomy.** A remote tool's own annotations are advisory. A tool the gateway marks
  non-destructive is still A2 here unless the profile says otherwise; the cell, not the
  gateway, is accountable for what it does in the tenant's name.
* **Classification.** A remote result's sensitivity is unknown, so it is C4 unless the
  gateway declares otherwise in a way we understand.

Transports: streamable HTTP (the modern default), SSE (legacy) and stdio (local servers).
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Literal

from agent_forge.connectors.base import (
    CallContext,
    CircuitBreaker,
    ConnectorBase,
    ToolResult,
    ToolSpec,
)
from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import Classification
from agent_forge.core.errors import ToolError
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = ["McpGatewayConnector", "McpServerConfig", "Transport"]

Transport = Literal["streamable_http", "sse", "stdio"]
PREFIX = "mcp"


@dataclass(slots=True)
class McpServerConfig:
    """One MCP server behind the gateway, or the gateway itself."""

    name: str
    transport: Transport = "streamable_http"
    url: str = ""
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    token: str = ""
    timeout: float = 30.0

    def validate(self) -> None:
        if self.transport == "stdio" and not self.command:
            raise ToolError("stdio MCP server needs a command", server=self.name)
        if self.transport in {"streamable_http", "sse"} and not self.url:
            raise ToolError("HTTP MCP server needs a url", server=self.name)


class McpGatewayConnector(ConnectorBase):
    """Exposes the tools of one or more MCP servers as this cell's tools."""

    def __init__(
        self,
        servers: Sequence[McpServerConfig] = (),
        *,
        name: str = PREFIX,
        default_autonomy: AutonomyLevel = AutonomyLevel.A2,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(name=name, version="1.0.0", timeout=timeout, breaker=CircuitBreaker())
        self._servers = list(servers)
        self._default_autonomy = default_autonomy
        self._specs: list[ToolSpec] = []
        self._routes: dict[str, tuple[McpServerConfig, str]] = {}
        self._connected = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> McpGatewayConnector:
        """Build from ``MCP_GATEWAY_URL``. Used by the entry point."""
        source = env if env is not None else dict(os.environ)
        url = source.get("MCP_GATEWAY_URL", "")
        if not url:
            # No gateway configured: a connector with no tools, which the registry will
            # find unhealthy and skip. Better than refusing to start the whole cell.
            return cls(())
        return cls(
            [
                McpServerConfig(
                    name="gateway",
                    transport="streamable_http",
                    url=url,
                    token=source.get("MCP_GATEWAY_TOKEN", ""),
                )
            ]
        )

    # ------------------------------------------------------------- discovery

    async def connect(self) -> int:
        """Call ``tools/list`` on every server and build the catalogue."""
        self._specs = []
        self._routes = {}
        for server in self._servers:
            server.validate()
            try:
                remote = await self._list_tools(server)
            except Exception as exc:
                log.warning("mcp.server_unreachable", server=server.name, detail=describe(exc))
                continue
            for tool in remote:
                spec = self._to_spec(server, tool)
                self._specs.append(spec)
                self._routes[spec.name] = (server, str(tool.get("name", "")))
        self._connected = True
        log.info("mcp.connected", servers=len(self._servers), tools=len(self._specs))
        return len(self._specs)

    def _to_spec(self, server: McpServerConfig, tool: dict[str, Any]) -> ToolSpec:
        """Translate a remote tool declaration into a local one, conservatively."""
        remote_name = str(tool.get("name", "")).strip()
        annotations = tool.get("annotations") or {}
        readonly = bool(annotations.get("readOnlyHint", False))
        # A read-only hint may lower the requirement to A0; anything else stays at the
        # configured default. The gateway's opinion is advisory, never authoritative.
        autonomy = AutonomyLevel.A0 if readonly else self._default_autonomy
        return ToolSpec(
            name=f"{self.name}.{server.name}.{remote_name}",
            description=str(tool.get("description") or remote_name),
            input_schema=dict(tool.get("inputSchema") or {}),
            required_scope=f"mcp:{server.name}:{remote_name}",
            autonomy_min=autonomy,
            # Unknown sensitivity: C4 until the gateway tells us otherwise.
            max_classification=Classification.C4,
            readonly=readonly,
        )

    def capabilities(self) -> Sequence[ToolSpec]:
        return tuple(self._specs)

    async def health(self) -> bool:
        if not self._servers:
            return False
        if not self._connected:
            try:
                await self.connect()
            except Exception:
                return False
        return bool(self._specs)

    # --------------------------------------------------------------- invoke

    async def invoke(self, tool: str, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        route = self._routes.get(tool)
        if route is None:
            return ToolResult.failure(f"unknown MCP tool {tool}")
        server, remote_name = route

        async def call() -> dict[str, Any]:
            return await self._call_tool(server, remote_name, args, ctx)

        # C4 by default: the gateway does not tell us how sensitive its answer is, and
        # guessing low is the one guess that leaks.
        return await self.run(tool, call, classification=Classification.C4)

    # ------------------------------------------------------------- transport

    async def _session(self, server: McpServerConfig, stack: AsyncExitStack) -> Any:
        """Open an MCP session over the configured transport."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        if server.transport == "stdio":
            params = StdioServerParameters(
                command=server.command, args=list(server.args), env=server.env or None
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        elif server.transport == "sse":
            from mcp.client.sse import sse_client

            read, write = await stack.enter_async_context(
                sse_client(server.url, headers=self._headers(server))
            )
        else:
            from mcp.client.streamable_http import streamable_http_client
            from mcp.shared._httpx_utils import create_mcp_http_client

            # The transport takes an HTTP client rather than a headers mapping, and the
            # MCP SDK pins its own httpx build -- so the client comes from its factory,
            # not from ours.
            http_client = await stack.enter_async_context(
                create_mcp_http_client(headers=self._headers(server))
            )
            read, write = await stack.enter_async_context(
                streamable_http_client(server.url, http_client=http_client)
            )

        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    @staticmethod
    def _headers(server: McpServerConfig) -> dict[str, str]:
        return {"authorization": f"Bearer {server.token}"} if server.token else {}

    async def _list_tools(self, server: McpServerConfig) -> list[dict[str, Any]]:
        async with AsyncExitStack() as stack:
            session = await self._session(server, stack)
            listing = await session.list_tools()
            return [
                {
                    "name": tool.name,
                    "description": tool.description,
                    # MCP 2.x renamed this from `inputSchema`; keep the wire name in
                    # our own dict so the spec builder stays transport-agnostic.
                    "inputSchema": getattr(tool, "input_schema", None)
                    or getattr(tool, "inputSchema", None)
                    or {},
                    "annotations": _annotations(tool),
                }
                for tool in listing.tools
            ]

    async def _call_tool(
        self, server: McpServerConfig, tool: str, args: dict[str, Any], ctx: CallContext
    ) -> dict[str, Any]:
        async with AsyncExitStack() as stack:
            session = await self._session(server, stack)
            response = await session.call_tool(tool, arguments={**args, **_identity_arguments(ctx)})
            # MCP 2.x renamed `isError` to `is_error`; both are checked because a
            # gateway may be running either SDK generation, and treating a failed remote
            # call as a success is how the agent ends up claiming it did something.
            if getattr(response, "is_error", None) or getattr(response, "isError", None):
                raise ToolError(
                    "MCP server reported an error", tool=tool, detail=_error_text(response)
                )
            return {"content": _content(response), "tool": tool, "server": server.name}


def _error_text(response: Any) -> str:
    """The server's own error message, so the failure is actionable."""
    return " ".join(
        str(block.get("text", "")) for block in _content(response) if block.get("type") == "text"
    )[:300]


def describe(exc: BaseException, *, depth: int = 0) -> str:
    """Flatten an exception, including groups, into one readable line.

    The MCP transports run inside task groups, so a connection failure surfaces as an
    ``ExceptionGroup``. Logging just its class name tells an operator nothing about why
    the gateway is unreachable.
    """
    label = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
    nested = getattr(exc, "exceptions", None)
    if nested and depth < 3:
        inner = "; ".join(describe(e, depth=depth + 1) for e in nested[:3])
        return f"{label} [{inner}]"
    return label[:300]


def _annotations(tool: Any) -> dict[str, Any]:
    raw = getattr(tool, "annotations", None)
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    return {
        key: getattr(raw, key)
        for key in ("readOnlyHint", "destructiveHint", "idempotentHint")
        if getattr(raw, key, None) is not None
    }


def _identity_arguments(ctx: CallContext) -> dict[str, Any]:
    """Propagate the end user's identity so the gateway can enforce its own scopes.

    Sent under an ``_agentforge`` key rather than merged into the tool's own arguments: a
    remote tool must not be able to declare a parameter called ``user_id`` and receive
    something it can spoof back.
    """
    return {
        "_agentforge": {
            "tenant_id": ctx.tenant_id,
            "user_id": ctx.user_id,
            "groups": list(ctx.groups),
            "trace_id": ctx.trace_id,
        }
    }


def _content(response: Any) -> list[dict[str, Any]]:
    """Flatten MCP content blocks into something JSON-serialisable."""
    out: list[dict[str, Any]] = []
    for block in getattr(response, "content", []) or []:
        kind = getattr(block, "type", "text")
        if kind == "text":
            out.append({"type": "text", "text": getattr(block, "text", "")})
        else:
            out.append({"type": kind, "data": _safe(block)})
    return out


def _safe(block: Any) -> Any:
    try:
        return json.loads(json.dumps(block, default=str))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(block)
