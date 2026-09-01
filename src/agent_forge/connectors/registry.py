"""Connector registry: discovery, allowlisting and the gate before every tool call.

Two checks, deliberately duplicated:

* **Before the catalogue is built**, so a tool the tenant may not use is never even
  offered to the model. A model cannot misuse a tool it has not been told about.
* **Immediately before invoking**, because time passes between the two and the policy can
  change. The second check is the one that actually protects the system; the first is
  what keeps the model from trying.

Adding a connector is an entry point and a `uv sync`. The core is never edited.
"""

from __future__ import annotations

import fnmatch
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

from agent_forge.connectors.base import (
    ENTRY_POINT_GROUP,
    BaseConnector,
    CallContext,
    ToolResult,
    ToolSpec,
    digest,
)
from agent_forge.core.autonomy import AutonomyLevel, AutonomyMap
from agent_forge.core.classification import Classification
from agent_forge.core.errors import AgentForgeError, AuthorizationError
from agent_forge.core.subgraphs.base import ToolDescriptor
from agent_forge.observability.logging import get_logger
from agent_forge.observability.metrics import get_metrics

log = get_logger(__name__)

# How long a health verdict is trusted before the connector is probed again.
HEALTH_TTL_SECONDS = 60.0

# Called before a tool runs, so F6 can insert the PDP without touching this module.
# (tool, args, ctx) -> allow
PolicyHook = Callable[[str, dict[str, Any], CallContext], Awaitable[bool]]


class ConnectorError(AgentForgeError):
    """A connector could not be loaded or is misconfigured."""

    code = "connector_unavailable"


@dataclass(slots=True)
class Allowlist:
    """Per-tenant tool allowlist from the profile, as glob patterns.

    An empty allowlist means **nothing is allowed**, not everything. A profile that
    forgets to list its tools gets an agent that cannot act, which is the safe direction
    to fail in.
    """

    patterns: tuple[str, ...] = ()
    # Tools the cell itself provides (repo_graph and the like) are always available:
    # they read the cell's own state, not a tenant's systems.
    builtin: frozenset[str] = frozenset()

    def permits(self, tool: str) -> bool:
        if tool in self.builtin:
            return True
        return any(fnmatch.fnmatch(tool, pattern) for pattern in self.patterns)

    def filter(self, specs: Iterable[ToolSpec]) -> list[ToolSpec]:
        return [spec for spec in specs if self.permits(spec.name)]


@dataclass(slots=True)
class _Registered:
    connector: BaseConnector
    healthy: bool = False
    checked_at: float = 0.0


@dataclass(slots=True)
class ToolInvocationRecord:
    """What happened, for the trace and the evidence ledger."""

    tool: str
    connector: str
    ok: bool
    autonomy_required: AutonomyLevel
    classification: Classification
    args_digest: str
    duration_ms: int
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "connector": self.connector,
            "ok": self.ok,
            "autonomy_required": str(self.autonomy_required),
            "classification": str(self.classification),
            "args_digest": self.args_digest,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


class ConnectorRegistry:
    """Discovers connectors, publishes their tools and mediates every call."""

    def __init__(
        self,
        *,
        allowlist: Allowlist | None = None,
        autonomy_map: AutonomyMap | None = None,
        policy: PolicyHook | None = None,
        health_ttl: float = HEALTH_TTL_SECONDS,
    ) -> None:
        self._connectors: dict[str, _Registered] = {}
        self._allowlist = allowlist or Allowlist()
        self._autonomy = autonomy_map or AutonomyMap()
        self._policy = policy
        self._health_ttl = health_ttl
        self.invocations: list[ToolInvocationRecord] = []

    # ------------------------------------------------------------ registration

    def register(self, connector: BaseConnector) -> None:
        """Add a connector instance. Idempotent by name."""
        if not getattr(connector, "name", ""):
            raise ConnectorError("connector must declare a name", cls=type(connector).__name__)
        self._connectors[connector.name] = _Registered(connector=connector)
        log.info(
            "connector.registered",
            connector=connector.name,
            version=getattr(connector, "version", "?"),
        )

    def discover(self, *, group: str = ENTRY_POINT_GROUP, only: Sequence[str] = ()) -> list[str]:
        """Load connector classes from entry points.

        A plugin that fails to import is an error, not a silent omission: an operator who
        installed a connector needs to know it did not load.
        """
        loaded: list[str] = []
        for entry in entry_points(group=group):
            if only and entry.name not in only:
                continue
            try:
                factory = entry.load()
            except Exception as exc:
                raise ConnectorError(
                    "connector entry point failed to import",
                    plugin=entry.name,
                    detail=str(exc),
                ) from exc
            try:
                instance = factory() if callable(factory) else factory
            except Exception as exc:
                raise ConnectorError(
                    "connector could not be constructed", plugin=entry.name, detail=str(exc)
                ) from exc
            self.register(instance)
            loaded.append(entry.name)
        return loaded

    def names(self) -> list[str]:
        return sorted(self._connectors)

    def get(self, name: str) -> BaseConnector:
        registered = self._connectors.get(name)
        if registered is None:
            raise ConnectorError("unknown connector", requested=name, known=self.names())
        return registered.connector

    # ----------------------------------------------------------------- health

    async def check_health(self, *, force: bool = False) -> dict[str, bool]:
        """Probe every connector, caching the verdict briefly."""
        now = time.monotonic()
        results: dict[str, bool] = {}
        for name, registered in self._connectors.items():
            if not force and now - registered.checked_at < self._health_ttl:
                results[name] = registered.healthy
                continue
            try:
                healthy = bool(await registered.connector.health())
            except Exception as exc:
                log.warning("connector.health_failed", connector=name, detail=type(exc).__name__)
                healthy = False
            registered.healthy = healthy
            registered.checked_at = now
            results[name] = healthy
        return results

    # ------------------------------------------------------------- catalogue

    async def catalogue(self, ctx: CallContext) -> list[ToolSpec]:
        """The tools this requester may be offered.

        Excluded: connectors that are unhealthy, tools outside the tenant allowlist, and
        tools whose results could exceed the requester's classification ceiling.
        """
        health = await self.check_health()
        specs: list[ToolSpec] = []
        for name, registered in self._connectors.items():
            if not health.get(name, False):
                continue
            for spec in registered.connector.capabilities():
                if not self._allowlist.permits(spec.name):
                    continue
                if spec.max_classification > ctx.classification_ceiling and spec.readonly:
                    # A read tool that can only return material above the ceiling is
                    # useless to this requester and its mere presence is informative.
                    continue
                specs.append(spec)
        specs.sort(key=lambda s: s.name)
        return specs

    async def descriptors(self, ctx: CallContext) -> list[ToolDescriptor]:
        """The catalogue in the shape a domain subgraph sees.

        Subgraphs get a name, a description and an autonomy level -- never a scope, an
        endpoint or a credential.
        """
        return [
            ToolDescriptor(
                name=spec.name,
                description=spec.description,
                autonomy_min=spec.autonomy_min,
                input_schema=spec.input_schema,
            )
            for spec in await self.catalogue(ctx)
        ]

    def effective_autonomy(self, tool: str, *, action_category: str | None = None) -> AutonomyLevel:
        """Maximum of the tool's own declaration and the profile's category map."""
        spec = self.find_spec(tool)
        declared = spec.autonomy_min if spec else AutonomyLevel.A4
        return self._autonomy.effective(action_category, declared=declared)

    def find_spec(self, tool: str) -> ToolSpec | None:
        for registered in self._connectors.values():
            for spec in registered.connector.capabilities():
                if spec.name == tool:
                    return spec
        return None

    # ------------------------------------------------------------------ invoke

    async def invoke(self, tool: str, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        """Run a tool after re-checking everything the catalogue checked.

        The catalogue decided what to offer; this decides what may run. Between the two
        the allowlist can change, the connector can go down and the PDP can revoke.
        """
        args_digest = digest(args)
        spec = self.find_spec(tool)
        if spec is None:
            return self._record(
                tool, None, ToolResult.failure("unknown tool"), args_digest, ctx.tenant_id
            )

        if not self._allowlist.permits(tool):
            log.warning("connector.tool_not_allowlisted", tool=tool, tenant_id=ctx.tenant_id)
            raise AuthorizationError("tool is not in this tenant's allowlist", tool=tool)

        health = await self.check_health()
        if not health.get(spec.connector, False):
            return self._record(
                tool,
                spec,
                ToolResult.failure("connector is not healthy"),
                args_digest,
                ctx.tenant_id,
            )

        if self._policy is not None and not await self._policy(tool, args, ctx):
            raise AuthorizationError("policy denied this tool call", tool=tool)

        required = self.effective_autonomy(tool)
        if required > ctx.autonomy_granted and required < AutonomyLevel.A2:
            # A2+ is handled by the graph's HITL pause before it reaches here; anything
            # below that which still outranks the grant is simply refused.
            raise AuthorizationError(
                "action outranks the autonomy granted to this requester",
                tool=tool,
                required=str(required),
                granted=str(ctx.autonomy_granted),
            )

        try:
            result = await self.get(spec.connector).invoke(tool, args, ctx)
        except AgentForgeError as exc:
            return self._record(
                tool, spec, ToolResult.failure(exc.message), args_digest, ctx.tenant_id
            )
        except Exception as exc:
            log.exception("connector.unhandled_error", tool=tool)
            return self._record(
                tool,
                spec,
                ToolResult.failure(f"unhandled: {type(exc).__name__}"),
                args_digest,
                ctx.tenant_id,
            )

        return self._record(tool, spec, result, args_digest, ctx.tenant_id)

    def _record(
        self,
        tool: str,
        spec: ToolSpec | None,
        result: ToolResult,
        args_digest: str,
        tenant_id: str = "",
    ) -> ToolResult:
        """Log, count and return one invocation's outcome.

        Every path out of ``invoke`` funnels through here, which is why the counter lives
        here: a metric incremented at four call sites is a metric that misses the fifth.
        """
        record = ToolInvocationRecord(
            tool=tool,
            connector=spec.connector if spec else tool.split(".", 1)[0],
            ok=result.ok,
            autonomy_required=spec.autonomy_min if spec else AutonomyLevel.A4,
            classification=result.classification,
            args_digest=args_digest,
            duration_ms=result.duration_ms,
            error=result.error,
        )
        self.invocations.append(record)
        get_metrics().tool_calls.labels(
            tenant=tenant_id or "_",
            connector=record.connector,
            tool=tool,
            outcome="ok" if result.ok else "error",
        ).inc()
        log.info("connector.invoked", **record.to_dict())
        return result

    # --------------------------------------------------------------- graph hook

    def executor(self, ctx_factory: Callable[[Any], CallContext]) -> Any:
        """The callable ``GraphDeps.tool_executor`` expects.

        Bridges the graph's state to a ``CallContext`` so the graph never constructs one
        itself and cannot get the entitlements wrong.
        """

        async def execute(tool: str, args: dict[str, Any], state: Any) -> dict[str, Any]:
            try:
                result = await self.invoke(tool, args, ctx_factory(state))
            except AuthorizationError as exc:
                return {"ok": False, "error": exc.message, "classification": "C0"}
            return {
                "ok": result.ok,
                "data": result.data,
                "error": result.error,
                "classification": str(result.classification),
                "evidence_digest": result.evidence_digest,
            }

        return execute

    async def aclose(self) -> None:
        for registered in self._connectors.values():
            try:
                await registered.connector.aclose()
            except Exception as exc:
                log.warning(
                    "connector.close_failed",
                    connector=registered.connector.name,
                    detail=type(exc).__name__,
                )

    def stats(self) -> dict[str, Any]:
        return {
            "connectors": self.names(),
            "healthy": [n for n, r in self._connectors.items() if r.healthy],
            "invocations": len(self.invocations),
            "failures": sum(1 for i in self.invocations if not i.ok),
        }


def context_from_state(state: Any) -> CallContext:
    """Build a ``CallContext`` from an ``AgentState``.

    The single translation point, for the same reason ``build_filter`` is: entitlements
    must not be assembled ad hoc at each call site.
    """
    identity = state.identity
    return CallContext(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id or "",
        groups=tuple(identity.groups),
        classification_ceiling=identity.classification_ceiling,
        trace_id=state.trace_id,
        task_id=state.task_id,
        autonomy_granted=state.autonomy,
    )


def build_registry(
    *,
    profile: Any,
    autonomy_map: AutonomyMap | None = None,
    policy: PolicyHook | None = None,
    connectors: Sequence[BaseConnector] = (),
    discover: bool = False,
) -> ConnectorRegistry:
    """Assemble the registry from the profile."""
    allowlist = Allowlist(
        patterns=tuple(profile.connectors.mcp_gateway.allowlist),
        builtin=frozenset({"repo_graph.query"}),
    )
    registry = ConnectorRegistry(
        allowlist=allowlist,
        autonomy_map=autonomy_map or profile.autonomy_map(),
        policy=policy,
    )
    for connector in connectors:
        registry.register(connector)
    if discover:
        registry.discover()
    return registry


__all__ = [
    "Allowlist",
    "ConnectorError",
    "ConnectorRegistry",
    "PolicyHook",
    "ToolInvocationRecord",
    "build_registry",
    "context_from_state",
]
