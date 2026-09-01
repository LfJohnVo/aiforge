"""Builders for a whole cell: a real Runtime whose only fakes are at the boundaries.

Separate from ``tests.support`` on purpose. That module holds protocol doubles and is
cheap to import; this one pulls in FastAPI, LangGraph and the knowledge stack, and only
the tests that exercise a complete cell should pay for that.

Everything here is shared rather than copied because the wiring is the thing under test:
three test modules assembling a Runtime three slightly different ways would each be
testing a cell nobody deploys.
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from agent_forge.core.autonomy import AutonomyMap
from agent_forge.core.classification import Classification
from agent_forge.core.state import AgentState
from agent_forge.core.subgraphs.base import ToolDescriptor
from tests.support import (
    REPO_ROOT,
    FakeTransport,
    make_deps,
    make_gateway,
    make_policy,
    make_prompts,
)

TENANT = "acme-mx"
API_KEY = "test-key-abcdef"
PUBLIC_URL = "https://cell.example.test"
AUTH = {"authorization": f"Bearer {API_KEY}"}


def cell_env(**extra: str) -> dict[str, str]:
    """The environment a test cell runs with. Loopback everywhere, no real secrets."""
    base = {
        "AGENT_FORGE_PROFILE": str(REPO_ROOT / "configs" / "agent.profile.example.yaml"),
        "AGENT_FORGE_ENV": "development",
        "AGENT_FORGE_INSTANCE": "test",
        "AGENT_API_KEYS": f"{TENANT}:{API_KEY}",
        "LOG_LEVEL": "WARNING",
        "MCP_GATEWAY_URL": "http://gw",
        "N8N_URL": "http://n8n",
        # No remote PDP: a test cell decides on the local Rego base. Pointing it at a
        # host that is not there would make every test wait for a timeout and then
        # fail closed, which tests the timeout rather than the cell.
        "GOVERNANCE_PDP_URL": "",
        "NATS_URL": "nats://nats:4222",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel:4317",
        "LITELLM_MASTER_KEY": "sk-test",
        "AGENT_PUBLIC_URL": PUBLIC_URL,
        "MCP_ALLOWED_HOSTS": "test",
    }
    base.update(extra)
    return base


def empty_knowledge() -> Any:
    """A real knowledge service over an empty corpus.

    Empty rather than absent: a test should take the same path a deployed cell takes,
    and "no corpus yet" is the normal state on day one.
    """
    from agent_forge.knowledge import CagPreloader, Classifier, IngestionPipeline, KnowledgeService
    from agent_forge.knowledge.rag import HashingEmbeddings, HybridRetriever, InMemoryVectorStore

    store = InMemoryVectorStore()
    embeddings = HashingEmbeddings(dimension=64)
    return KnowledgeService(
        retriever=HybridRetriever(store, embeddings),
        pipeline=IngestionPipeline(
            vector_store=store,
            embeddings=embeddings,
            classifier=Classifier(default=Classification.C2, use_model=False),
        ),
        cag=CagPreloader(store=store),
        vector_store=store,
    )


def _with_surfaces(profile: Any, **flags: bool) -> Any:
    """Return a copy of the profile with the named surfaces switched on or off.

    Profile models are frozen, so this rebuilds the two sections rather than assigning.
    """
    from agent_forge.profile.models import ChannelToggle

    # `model_copy(update=...)` does not re-validate, so only the toggled fields are
    # replaced and they are replaced with models, never with dicts.
    toggles = {name: ChannelToggle(enabled=on) for name, on in flags.items()}
    return profile.model_copy(
        update={
            "channels": profile.channels.model_copy(
                update={k: v for k, v in toggles.items() if hasattr(profile.channels, k)}
            ),
            "upstream": profile.upstream.model_copy(
                update={k: v for k, v in toggles.items() if hasattr(profile.upstream, k)}
            ),
        }
    )


def make_runtime(
    transport: FakeTransport | None = None,
    *,
    subgraph: Any | None = None,
    autonomy_map: AutonomyMap | None = None,
    tool_executor: Any | None = None,
    tool_catalog: Sequence[ToolDescriptor] = (),
    env: dict[str, str] | None = None,
    enable: Sequence[str] = (),
) -> Any:
    """A Runtime wired to fakes: no LiteLLM transport, no Postgres, real graph.

    ``enable`` switches surfaces on by name (``teams``, ``slack``, ``mcp_server``) so a
    test does not need its own profile file. The MCP server is off unless asked for: it
    runs an anyio task group, and a fixture that enters it in one task and leaves it in
    another -- which is what an async generator fixture does -- trips anyio's cancel
    scope check. Tests that want it drive the lifespan inside a single coroutine.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    from agent_forge.api.auth import Authenticator, AuthSettings
    from agent_forge.connectors import Allowlist, ConnectorRegistry
    from agent_forge.core.graph import build_graph
    from agent_forge.core.hitl import ApprovalPolicy, InMemoryApprovalStore
    from agent_forge.events import Aggregator, InMemoryBus, build_ledger
    from agent_forge.governance import build_governance
    from agent_forge.memory import InMemoryStore, build_memory
    from agent_forge.observability import Observability, build_langfuse, get_metrics
    from agent_forge.profile import load_profile
    from agent_forge.runtime import ChannelSettings, Runtime, Settings
    from agent_forge.upstream.tasks import TaskRunner

    environment = env or cell_env()
    profile = load_profile(environment["AGENT_FORGE_PROFILE"], env=environment)
    profile = _with_surfaces(profile, **{"mcp_server": False, **dict.fromkeys(enable, True)})

    gateway = make_gateway(transport or FakeTransport())
    memory = build_memory(store=InMemoryStore(), cache_similarity=0.9)
    bus = InMemoryBus()
    # A temporary ledger per cell: a test must not append to the repository's own chain,
    # and two tests sharing one would see each other's records.
    ledger = build_ledger(Path(tempfile.mkdtemp(prefix="agent-forge-ledger-")), bus=bus)
    governance = build_governance(profile)
    knowledge = empty_knowledge()
    registry = ConnectorRegistry(allowlist=Allowlist(builtin=frozenset({"repo_graph.query"})))
    deps = make_deps(
        gateway=gateway,
        subgraph=subgraph,
        autonomy_map=autonomy_map,
        tool_executor=tool_executor,
        tool_catalog=tool_catalog,
    )
    deps.memory = memory
    deps.knowledge = knowledge
    deps.governance = governance.gate
    graph = build_graph(deps, checkpointer=InMemorySaver())

    async def invoke(state: AgentState) -> AgentState:
        thread = f"{state.identity.tenant_id}:{state.thread_id}"
        raw = await graph.ainvoke(state, config={"configurable": {"thread_id": thread}})
        if isinstance(raw, AgentState):
            return raw
        payload = {k: v for k, v in raw.items() if not k.startswith("__")}
        final = AgentState.model_validate(payload)
        # An interrupt means the graph paused for a human; upstream must see that as
        # `input-required`, not as a task that quietly finished.
        if raw.get("__interrupt__"):
            return final.model_copy(update={"status": "awaiting_approval"})
        return final

    return Runtime(
        settings=Settings.from_env(environment),
        profile=profile,
        prompts=make_prompts(),
        policy=make_policy(),
        gateway=gateway,
        deps=deps,
        graph=graph,
        authenticator=Authenticator(AuthSettings.from_env(environment)),
        memory=memory,
        knowledge=knowledge,
        connectors=registry,
        tasks=TaskRunner(
            invoke, agent_name=profile.identity.agent_name, area=profile.identity.area
        ),
        observability=Observability(metrics=get_metrics(), langfuse=build_langfuse(enabled=False)),
        governance=governance,
        bus=bus,
        ledger=ledger,
        aggregator=Aggregator(bus=bus, tenant_id=profile.identity.tenant_id, ledger=ledger),
        channel_settings=ChannelSettings.from_env(environment),
        approvals=InMemoryApprovalStore(),
        approval_policy=ApprovalPolicy(approvers_group="finanzas-lideres"),
        capabilities={"knowledge": False, "memory": False},
    )


def make_app(runtime: Any, env: dict[str, str] | None = None) -> Any:
    """The real FastAPI app, with the given runtime injected instead of the built one.

    Building the app for real matters: the surfaces under test are mounted by
    ``_mount_channels``, so a hand-assembled router would prove nothing about what a
    deployed cell actually exposes.
    """
    from fastapi import FastAPI

    from agent_forge.api.app import _mount_channels, create_app

    environment = env or cell_env()
    app = create_app(settings=runtime.settings, env=environment)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            app.state.runtime = runtime
            await _mount_channels(app, runtime, stack)
            yield

    app.router.lifespan_context = lifespan
    return app
