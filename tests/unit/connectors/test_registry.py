"""The registry: allowlisting, health, autonomy and the gate before every call.

The registry is where a connector's honest declaration becomes an enforced rule. What is
tested here is mostly refusal: the catalogue that leaves a tool out, the call that is
rejected between offer and execution, the connector that is skipped because it is down.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from agent_forge.connectors import (
    Allowlist,
    CallContext,
    CircuitBreaker,
    ConnectorBase,
    ConnectorError,
    ConnectorRegistry,
    ToolResult,
    ToolSpec,
    context_from_state,
)
from agent_forge.connectors.base import guard
from agent_forge.core.autonomy import AutonomyLevel, AutonomyMap
from agent_forge.core.classification import Classification
from agent_forge.core.errors import AuthorizationError, ToolError
from tests.support import make_state

CTX = CallContext(
    tenant_id="acme-mx",
    user_id="u1",
    groups=("finanzas",),
    classification_ceiling=Classification.C2,
    autonomy_granted=AutonomyLevel.A1,
)


class FakeConnector(ConnectorBase):
    """A connector whose behaviour the test dictates."""

    def __init__(
        self,
        name: str = "jira",
        *,
        healthy: bool = True,
        specs: Sequence[ToolSpec] | None = None,
        result: ToolResult | None = None,
        raises: Exception | None = None,
    ) -> None:
        super().__init__(name=name, version="1.0.0")
        self.healthy = healthy
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self._raises = raises
        self._result = result or ToolResult.success({"ok": True}, classification=Classification.C1)
        self._specs = tuple(
            specs
            or (
                ToolSpec(
                    name=f"{name}.read_issue",
                    description="Lee una incidencia",
                    required_scope=f"{name}:read",
                    autonomy_min=AutonomyLevel.A0,
                    max_classification=Classification.C1,
                    readonly=True,
                ),
                ToolSpec(
                    name=f"{name}.create_issue",
                    description="Crea una incidencia",
                    required_scope=f"{name}:write",
                    autonomy_min=AutonomyLevel.A2,
                    max_classification=Classification.C2,
                ),
            )
        )

    def capabilities(self) -> Sequence[ToolSpec]:
        return self._specs

    async def health(self) -> bool:
        return self.healthy

    async def invoke(self, tool: str, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        del ctx
        self.calls.append((tool, args))
        if self._raises is not None:
            raise self._raises
        return self._result

    async def aclose(self) -> None:
        self.closed = True


def registry(**kwargs: Any) -> ConnectorRegistry:
    kwargs.setdefault("allowlist", Allowlist(patterns=("jira.*",)))
    return ConnectorRegistry(**kwargs)


# ------------------------------------------------------------------- allowlist


async def test_only_allowlisted_tools_are_offered() -> None:
    reg = ConnectorRegistry(allowlist=Allowlist(patterns=("jira.read_*",)))
    reg.register(FakeConnector())

    names = {spec.name for spec in await reg.catalogue(CTX)}

    assert names == {"jira.read_issue"}


async def test_an_empty_allowlist_offers_nothing() -> None:
    """A profile that forgets to list its tools gets an agent that cannot act."""
    reg = ConnectorRegistry(allowlist=Allowlist())
    reg.register(FakeConnector())

    assert await reg.catalogue(CTX) == []


async def test_a_builtin_tool_needs_no_allowlist_entry() -> None:
    reg = ConnectorRegistry(allowlist=Allowlist(builtin=frozenset({"jira.read_issue"})))
    reg.register(FakeConnector())

    names = {spec.name for spec in await reg.catalogue(CTX)}

    assert names == {"jira.read_issue"}


async def test_invoking_a_tool_outside_the_allowlist_is_refused() -> None:
    """The second check: policy can change between offering and calling."""
    reg = ConnectorRegistry(allowlist=Allowlist(patterns=("jira.read_*",)))
    connector = FakeConnector()
    reg.register(connector)

    with pytest.raises(AuthorizationError, match="allowlist"):
        await reg.invoke("jira.create_issue", {}, CTX)

    assert connector.calls == []


# ---------------------------------------------------------------------- health


async def test_an_unhealthy_connectors_tools_are_not_offered() -> None:
    reg = registry()
    reg.register(FakeConnector(healthy=False))

    assert await reg.catalogue(CTX) == []


async def test_an_unhealthy_connector_cannot_be_invoked() -> None:
    reg = registry()
    connector = FakeConnector(healthy=False)
    reg.register(connector)

    result = await reg.invoke("jira.read_issue", {}, CTX)

    assert not result.ok
    assert "not healthy" in (result.error or "")
    assert connector.calls == []


async def test_a_connector_whose_health_raises_is_treated_as_down() -> None:
    class Exploding(FakeConnector):
        async def health(self) -> bool:
            raise RuntimeError("boom")

    reg = registry()
    reg.register(Exploding())

    assert (await reg.check_health())["jira"] is False


async def test_health_is_cached_briefly() -> None:
    probes = 0

    class Counting(FakeConnector):
        async def health(self) -> bool:
            nonlocal probes
            probes += 1
            return True

    reg = registry(health_ttl=60.0)
    reg.register(Counting())

    await reg.check_health()
    await reg.check_health()

    assert probes == 1
    await reg.check_health(force=True)
    assert probes == 2


# -------------------------------------------------------------------- autonomy


def test_the_effective_level_is_the_maximum_of_tool_and_profile() -> None:
    reg = registry(
        autonomy_map=AutonomyMap(
            default=AutonomyLevel.A1, overrides={"gestionar_incidencia": AutonomyLevel.A3}
        )
    )
    reg.register(FakeConnector())

    assert reg.effective_autonomy("jira.read_issue") == AutonomyLevel.A1
    assert (
        reg.effective_autonomy("jira.read_issue", action_category="gestionar_incidencia")
        == AutonomyLevel.A3
    )


def test_an_unknown_tool_is_treated_as_maximally_privileged() -> None:
    """Failing closed: something we cannot identify does not get a low bar."""
    assert registry().effective_autonomy("desconocida.tool") == AutonomyLevel.A4


async def test_an_action_outranking_the_grant_is_refused_below_a2() -> None:
    reg = registry(autonomy_map=AutonomyMap(default=AutonomyLevel.A1))
    reg.register(FakeConnector())
    anonymous = CTX.model_copy(update={"autonomy_granted": AutonomyLevel.A0})

    with pytest.raises(AuthorizationError, match="outranks"):
        await reg.invoke("jira.read_issue", {}, anonymous)


# ----------------------------------------------------------------- invocation


async def test_a_successful_call_is_recorded_with_a_digest_not_the_arguments() -> None:
    reg = registry()
    reg.register(FakeConnector())

    result = await reg.invoke("jira.read_issue", {"secret": "no-debe-aparecer"}, CTX)

    assert result.ok
    record = reg.invocations[-1]
    assert record.args_digest
    assert "no-debe-aparecer" not in str(record.to_dict())


async def test_a_connector_exception_becomes_a_failed_result() -> None:
    reg = registry()
    reg.register(FakeConnector(raises=ToolError("backend down")))

    result = await reg.invoke("jira.read_issue", {}, CTX)

    assert not result.ok
    assert "backend down" in (result.error or "")


async def test_an_unexpected_exception_does_not_escape() -> None:
    reg = registry()
    reg.register(FakeConnector(raises=ValueError("unexpected")))

    result = await reg.invoke("jira.read_issue", {}, CTX)

    assert not result.ok
    assert "unhandled" in (result.error or "")


async def test_an_unknown_tool_returns_a_failure_not_an_exception() -> None:
    reg = registry()
    reg.register(FakeConnector())

    result = await reg.invoke("jira.no_existe", {}, CTX)

    assert not result.ok


async def test_a_policy_hook_can_veto_a_call() -> None:
    async def deny(tool: str, args: dict[str, Any], ctx: CallContext) -> bool:
        del tool, args, ctx
        return False

    reg = registry(policy=deny)
    connector = FakeConnector()
    reg.register(connector)

    with pytest.raises(AuthorizationError, match="policy"):
        await reg.invoke("jira.read_issue", {}, CTX)

    assert connector.calls == []


# ------------------------------------------------------------------ the graph


async def test_the_executor_shapes_results_for_the_graph() -> None:
    reg = registry()
    reg.register(FakeConnector())
    execute = reg.executor(context_from_state)

    payload = await execute("jira.read_issue", {}, make_state())

    assert payload["ok"] is True
    assert payload["classification"] == "C1"
    assert payload["evidence_digest"]


async def test_the_executor_turns_a_refusal_into_a_failed_call_not_a_crash() -> None:
    reg = ConnectorRegistry(allowlist=Allowlist())
    reg.register(FakeConnector())
    execute = reg.executor(context_from_state)

    payload = await execute("jira.read_issue", {}, make_state())

    assert payload["ok"] is False
    assert payload["classification"] == "C0"


def test_the_call_context_is_built_from_the_state_in_one_place() -> None:
    ctx = context_from_state(make_state())

    assert ctx.tenant_id == "acme-mx"
    assert ctx.groups == ("finanzas",)
    assert ctx.classification_ceiling == Classification.C2


async def test_descriptors_expose_no_scopes_or_endpoints() -> None:
    """A subgraph gets a name, a description and an autonomy level. Nothing else."""
    reg = registry()
    reg.register(FakeConnector())

    descriptors = await reg.descriptors(CTX)

    assert descriptors
    assert not hasattr(descriptors[0], "required_scope")


# ------------------------------------------------------------------ discovery


def test_registering_a_nameless_connector_is_rejected() -> None:
    class Nameless(FakeConnector):
        pass

    connector = Nameless()
    connector.name = ""

    with pytest.raises(ConnectorError, match="name"):
        registry().register(connector)


def test_an_unknown_connector_lookup_raises() -> None:
    with pytest.raises(ConnectorError, match="unknown connector"):
        registry().get("no-existe")


async def test_closing_the_registry_closes_every_connector() -> None:
    reg = registry()
    connector = FakeConnector()
    reg.register(connector)

    await reg.aclose()

    assert connector.closed


async def test_stats_report_health_and_failures() -> None:
    reg = registry()
    reg.register(FakeConnector(raises=ToolError("x")))
    await reg.invoke("jira.read_issue", {}, CTX)

    stats = reg.stats()

    assert stats["invocations"] == 1
    assert stats["failures"] == 1


def test_discovery_reports_a_broken_plugin_rather_than_skipping_it() -> None:
    """An operator who installed a connector needs to know it did not load."""
    from importlib.metadata import EntryPoint

    class BrokenEntryPoints:
        def __call__(self, *, group: str) -> list[EntryPoint]:
            del group
            return [EntryPoint(name="roto", value="no.such.module:Thing", group="g")]

    reg = registry()
    import agent_forge.connectors.registry as module

    original = module.entry_points
    module.entry_points = BrokenEntryPoints()  # type: ignore[assignment]
    try:
        with pytest.raises(ConnectorError, match="failed to import"):
            reg.discover()
    finally:
        module.entry_points = original  # type: ignore[assignment]


# ------------------------------------------------------------- circuit breaker


async def test_the_breaker_opens_after_repeated_failures() -> None:
    breaker = CircuitBreaker(threshold=2, cooldown=60.0)

    async def failing() -> str:
        raise ToolError("down")

    for _ in range(2):
        with pytest.raises(ToolError):
            await guard(failing, breaker=breaker, what="x")

    assert breaker.state() == "open"
    with pytest.raises(ToolError, match="circuit breaker"):
        await guard(failing, breaker=breaker, what="x")


async def test_the_breaker_half_opens_after_the_cooldown() -> None:
    breaker = CircuitBreaker(threshold=1, cooldown=-1.0)

    async def failing() -> str:
        raise ToolError("down")

    with pytest.raises(ToolError):
        await guard(failing, breaker=breaker, what="x")

    # Cooldown already elapsed: one call is let through to test the water.
    assert breaker.is_open is False


async def test_a_success_resets_the_breaker() -> None:
    breaker = CircuitBreaker(threshold=3)

    async def failing() -> str:
        raise ToolError("down")

    async def working() -> str:
        return "ok"

    with pytest.raises(ToolError):
        await guard(failing, breaker=breaker, what="x")
    assert await guard(working, breaker=breaker, what="x") == "ok"
    assert breaker.state() == "closed"


async def test_a_slow_call_times_out() -> None:
    import asyncio

    async def slow() -> str:
        await asyncio.sleep(5)
        return "never"

    with pytest.raises(ToolError, match="timed out"):
        await guard(slow, breaker=CircuitBreaker(), timeout=0.05, what="slow")


def test_a_tool_name_is_valid_for_a_model() -> None:
    spec = ToolSpec(name="jira.read_issue", description="x")

    assert "." not in spec.to_openai_tool()["function"]["name"]
