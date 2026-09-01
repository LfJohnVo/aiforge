"""Composition root: turns a profile plus an environment into a running cell.

Everything the process needs is built exactly once, here, and handed to the layers that
use it. No module reaches for global configuration, which is what makes two cells able to
run side by side in one host and what makes the whole thing testable.

Startup deliberately fails loudly on three things, because each of them is a silent
production problem otherwise: an invalid profile, a capability the profile needs whose
optional extra is not installed (ADR-005), and a model gateway that cannot be reached.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_forge.api.auth import Authenticator, AuthSettings
from agent_forge.connectors import ConnectorRegistry, build_registry, context_from_state
from agent_forge.connectors.databases import build_database_connectors
from agent_forge.connectors.mcp_client import McpGatewayConnector
from agent_forge.connectors.n8n import N8nConnector, WorkflowSpec
from agent_forge.connectors.repo_graph import RepoGraphConnector
from agent_forge.core.checkpointer import CheckpointerKind, open_checkpointer
from agent_forge.core.errors import CapabilityUnavailableError, ProfileError
from agent_forge.core.graph import GraphDeps, build_graph
from agent_forge.core.hitl import ApprovalPolicy, ApprovalStore, InMemoryApprovalStore
from agent_forge.core.planner import Planner
from agent_forge.core.prompts import PromptRegistry
from agent_forge.core.router import IntentRouter
from agent_forge.core.subgraphs.base import load_subgraph
from agent_forge.events import (
    Aggregator,
    EventBus,
    EvidenceLedger,
    LocalJudge,
    build_bus,
    build_ledger,
)
from agent_forge.gateway.litellm_client import GovernedGateway, LiteLLMTransport
from agent_forge.gateway.model_policy import ModelPolicy
from agent_forge.governance import GovernanceService, build_governance
from agent_forge.knowledge import KnowledgeService, build_knowledge
from agent_forge.memory import MemoryManager, Scrubber, build_memory
from agent_forge.observability.logging import configure_logging, get_logger
from agent_forge.profile import AgentProfile, load_profile
from agent_forge.upstream.tasks import TaskRunner

log = get_logger(__name__)

# Optional extras (ADR-005) mapped to an import that proves they are installed.
CAPABILITY_PROBES: dict[str, str] = {
    "knowledge": "qdrant_client",
    "memory": "mem0",
    "guardrails": "presidio_analyzer",
    "promptguard": "llm_guard",
    "databases": "sqlalchemy",
    "sources": "msgraph",
    "evals": "langfuse",
    "analytics": "statsforecast",
}


def installed_capabilities() -> dict[str, bool]:
    """Which optional extras this process actually has. Reported by ``/health``."""
    return {
        name: importlib.util.find_spec(module) is not None
        for name, module in CAPABILITY_PROBES.items()
    }


@dataclass(slots=True)
class Settings:
    """Environment-derived settings. The profile holds everything else."""

    profile_path: Path
    environment: str = "development"
    instance: str = "default"
    log_level: str = "INFO"
    log_format: str = "json"
    litellm_config: Path = Path("configs/litellm.yaml")
    prompts_dir: Path = Path("configs/prompts")
    postgres_dsn: str = ""
    redis_url: str = ""
    checkpointer: CheckpointerKind = "postgres"
    # Absolute base URL of this cell, as an orchestrator sees it. The A2A Agent Card
    # and the n8n callback both need it; a relative URL is useless to a remote caller.
    public_url: str = ""
    # Extra Host header values the MCP surface accepts, for a cell reachable under more
    # than one name. The public URL's own host is always allowed; this is the override
    # for the rest, and it stays a list rather than a wildcard because the check it
    # feeds is what stops DNS rebinding.
    mcp_allowed_hosts: tuple[str, ...] = ()
    # Where the evidence chain is written. A local path on purpose: the chain is the
    # cell's own record and must survive the central ledger being unreachable.
    ledger_path: str = "./var/ledger"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = env if env is not None else os.environ
        dsn = source.get("POSTGRES_DSN", "")
        # A cell with no Postgres still has to start on a laptop; it just cannot promise
        # that a paused approval survives a restart, and it says so in the log.
        kind: CheckpointerKind = "postgres" if dsn else "memory"
        return cls(
            profile_path=Path(
                source.get("AGENT_FORGE_PROFILE", "configs/agent.profile.example.yaml")
            ),
            environment=source.get("AGENT_FORGE_ENV", "development"),
            instance=source.get("AGENT_FORGE_INSTANCE", "default"),
            log_level=source.get("LOG_LEVEL", "INFO"),
            log_format=source.get("LOG_FORMAT", "json"),
            litellm_config=Path(source.get("LITELLM_CONFIG", "configs/litellm.yaml")),
            prompts_dir=Path(source.get("PROMPTS_DIR", "configs/prompts")),
            postgres_dsn=dsn,
            redis_url=source.get("REDIS_URL", ""),
            checkpointer=kind,
            public_url=source.get("AGENT_PUBLIC_URL", "").rstrip("/"),
            mcp_allowed_hosts=tuple(
                h.strip() for h in source.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
            ),
            ledger_path=source.get("LEDGER_PATH", "./var/ledger"),
        )

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@dataclass(slots=True)
class TeamsSettings:
    """Teams webhook configuration. Without ``app_id`` the channel refuses requests."""

    app_id: str = ""
    group_map: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class SlackSettings:
    """Slack webhook configuration. Without the signing secret nothing verifies."""

    signing_secret: str = ""
    bot_token: str = ""
    group_map: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class ChannelSettings:
    """Per-channel secrets and identity mappings, read from the environment."""

    teams: TeamsSettings = field(default_factory=TeamsSettings)
    slack: SlackSettings = field(default_factory=SlackSettings)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ChannelSettings:
        return cls(
            teams=TeamsSettings(
                app_id=env.get("TEAMS_APP_ID", ""),
                group_map=_group_map(env.get("TEAMS_GROUP_MAP", "")),
            ),
            slack=SlackSettings(
                signing_secret=env.get("SLACK_SIGNING_SECRET", ""),
                bot_token=env.get("SLACK_BOT_TOKEN", ""),
                group_map=_group_map(env.get("SLACK_GROUP_MAP", "")),
            ),
        )


def _group_map(raw: str) -> dict[str, list[str]]:
    """Parse ``user:group1|group2,user2:group3`` into a mapping.

    A user absent from the map ends up with no groups, which under identity-aware
    retrieval means they see nothing with an ACL. That is the intended default: the cell
    must not guess entitlements for someone the directory has not placed.
    """
    mapping: dict[str, list[str]] = {}
    for entry in raw.split(","):
        user, _, groups = entry.strip().partition(":")
        if user and groups:
            mapping[user.strip()] = [g.strip() for g in groups.split("|") if g.strip()]
    return mapping


@dataclass(slots=True)
class Runtime:
    """A fully wired cell."""

    settings: Settings
    profile: AgentProfile
    prompts: PromptRegistry
    policy: ModelPolicy
    gateway: GovernedGateway
    deps: GraphDeps
    graph: Any
    authenticator: Authenticator
    memory: MemoryManager
    knowledge: KnowledgeService
    connectors: ConnectorRegistry
    tasks: TaskRunner
    governance: GovernanceService
    bus: EventBus
    ledger: EvidenceLedger
    aggregator: Aggregator
    channel_settings: ChannelSettings
    approvals: ApprovalStore
    approval_policy: ApprovalPolicy
    capabilities: dict[str, bool] = field(default_factory=dict)

    @property
    def tenant_id(self) -> str:
        return self.profile.identity.tenant_id

    @property
    def instance_key(self) -> str:
        return f"{self.profile.instance_key}@{self.settings.instance}"

    async def aclose(self) -> None:
        await self.gateway.aclose()
        await self.memory.store.aclose()
        await self.knowledge.aclose()
        await self.connectors.aclose()
        await self.governance.aclose()
        await self.bus.aclose()


def _resume_hook(graph: Any, tasks: TaskRunner, instance: str) -> Any:
    """Build the callback the aggregator uses to act on a retry or replan verdict.

    Resumes from the checkpoint rather than re-running the task. The verdict arrives
    later, over a bus, quite possibly in a different process; re-running from scratch
    would repeat every tool call the task already made.

    The mechanism: write the verdict into the checkpointed state *as if* the quality gate
    had produced it, then continue. The graph's own conditional edge does the rest, which
    is why there is no second copy of the retry routing here.
    """

    async def resume(task_id: str, verdict: str, reasons: tuple[str, ...]) -> None:
        from agent_forge.core.checkpointer import namespaced_thread_id

        record = await _find_task(tasks, task_id)
        if record is None:
            log.warning("resume.unknown_task", task_id=task_id)
            return

        config = {
            "configurable": {
                "thread_id": namespaced_thread_id(record.tenant_id, instance, record.thread_id)
            }
        }
        update: dict[str, Any] = {
            "verdict": verdict,
            "scratchpad": {"judge_feedback": "; ".join(reasons) or f"veredicto {verdict}"},
        }
        if verdict == "replan":
            update["plan"] = []
        await graph.aupdate_state(config, update, as_node="quality_gate")
        await graph.ainvoke(None, config)
        log.info("resume.done", task_id=task_id, verdict=verdict)

    return resume


async def _find_task(tasks: TaskRunner, task_id: str) -> Any:
    """A verdict names a task, not a tenant, so the store is searched for the id."""
    store = tasks.store
    getter = getattr(store, "find", None)
    if getter is not None:  # pragma: no cover - for a store that can look up by id alone
        return await getter(task_id)
    records = getattr(store, "_records", {})
    for (_, candidate), record in records.items():
        if candidate == task_id:
            return record
    return None


def check_capabilities(profile: AgentProfile, *, strict: bool) -> dict[str, bool]:
    """Compare what the profile needs against what is installed.

    In production a missing extra is fatal: a cell that says it does GraphRAG and
    silently does not is worse than a cell that refuses to start. In development it is a
    warning, so the quickstart works without the heavy stack.
    """
    installed = installed_capabilities()
    missing = sorted(
        name for name in profile.required_capabilities() if not installed.get(name, False)
    )
    if not missing:
        return installed
    if strict:
        raise CapabilityUnavailableError(
            "the profile requires optional extras that are not installed",
            missing=missing,
            hint=f"uv sync {' '.join('--extra ' + m for m in missing)}",
        )
    log.warning(
        "runtime.capabilities_missing",
        missing=missing,
        detail="running with built-in fallbacks; see ADR-005",
    )
    return installed


def load_model_policy(path: Path) -> ModelPolicy:
    if not path.is_file():
        raise ProfileError("litellm config not found", path=str(path))
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ProfileError("litellm config must be a mapping", path=str(path))
    return ModelPolicy.from_litellm_config(config)


async def build_runtime(
    settings: Settings,
    stack: AsyncExitStack,
    *,
    env: Mapping[str, str] | None = None,
) -> Runtime:
    """Wire the whole cell. ``stack`` owns the lifetime of everything opened here."""
    source = env if env is not None else os.environ

    configure_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        service="agent-forge",
        instance=settings.instance,
    )

    profile = load_profile(settings.profile_path, env=dict(source))
    capabilities = check_capabilities(profile, strict=settings.is_production)

    prompts = PromptRegistry.from_directory(settings.prompts_dir)
    policy = load_model_policy(settings.litellm_config)
    gateway = GovernedGateway(
        LiteLLMTransport.from_env(source),
        policy,
        external_allowed=profile.models.external_allowed,
    )

    subgraph = load_subgraph(profile.domain.subgraph, profile.domain.intents_extra)
    planner = Planner(prompts, gateway=gateway, model_alias=profile.models.fast)
    router = IntentRouter(subgraph.intents, gateway=gateway, fast_alias=profile.models.fast)

    memory = build_memory(
        redis_url=source.get("REDIS_URL", ""),
        redis_password=source.get("REDIS_PASSWORD", ""),
        instance=settings.instance,
        scope=profile.memory.ltm.scope,
        ltm_enabled=profile.memory.ltm.enabled,
        episodic_enabled=profile.memory.episodic.enabled,
        stm_ttl_minutes=profile.memory.stm_ttl_minutes,
        cache_enabled=profile.memory.semantic_cache.enabled,
        cache_similarity=profile.memory.semantic_cache.similarity,
        cache_ttl_hours=profile.memory.semantic_cache.ttl_hours,
        retention_days=profile.memory.retention_days,
        scrubber=Scrubber(
            enabled=profile.governance.dlp.enabled, language=profile.identity.language
        ),
        gateway=gateway,
        fast_model=profile.models.fast,
    )

    knowledge = build_knowledge(profile=profile, gateway=gateway, env=dict(source))

    connectors = build_registry(
        profile=profile,
        connectors=[
            McpGatewayConnector.from_env(dict(source)),
            N8nConnector(
                source.get("N8N_URL", ""),
                api_key=source.get("N8N_API_KEY", ""),
                workflows=[
                    WorkflowSpec(name=name, description=f"Workflow n8n {name}")
                    for name in profile.connectors.n8n.workflows
                ],
                callback_url=(
                    f"{source.get('AGENT_PUBLIC_URL', '')}/channels/n8n/callback"
                    if source.get("AGENT_PUBLIC_URL")
                    else ""
                ),
            ),
            RepoGraphConnector(),
            *build_database_connectors(profile.connectors.databases, env=dict(source)),
        ],
    )

    # Governance and evidence come before the graph: the gate hook and the judge are
    # both dependencies of it, and the ledger has to exist before the first decision it
    # is meant to record.
    bus = build_bus(profile.events.fabric_url, source=profile.identity.agent_name)
    stack.push_async_callback(bus.aclose)
    ledger = build_ledger(settings.ledger_path, bus=bus, source=profile.identity.agent_name)

    async def record(action: str, actor: str, payload: dict[str, Any]) -> None:
        await ledger.record(
            action,
            actor,  # type: ignore[arg-type]
            payload,
            tenant_id=str(payload.get("tenant_id") or profile.identity.tenant_id),
            metadata={"task_id": payload.get("task_id", "")},
        )

    governance = build_governance(profile, ledger=record)
    judge = (
        LocalJudge(
            gateway=gateway,
            model=profile.models.quality,
            groundedness_threshold=profile.events.judge.thresholds.groundedness,
            safety_threshold=profile.events.judge.thresholds.safety,
            scan_answer=governance.scan_answer,
        )
        if profile.events.judge.mode == "local"
        else None
    )

    deps = GraphDeps(
        subgraph=subgraph,
        prompts=prompts,
        planner=planner,
        router=router,
        gateway=gateway,
        autonomy_map=profile.autonomy_map(),
        model_fast=profile.models.fast,
        model_quality=profile.models.quality,
        language=profile.identity.language,
        persona=profile.identity.persona,
        max_retries=profile.events.judge.max_retries,
        memory=memory,
        knowledge=knowledge,
        tool_executor=connectors.executor(context_from_state),
        governance=governance.gate,
        quality=judge,
    )

    checkpointer = await stack.enter_async_context(
        open_checkpointer(
            settings.checkpointer,
            postgres_dsn=settings.postgres_dsn,
            redis_url=settings.redis_url,
        )
    )
    graph = build_graph(deps, checkpointer=checkpointer)

    async def invoke(state: Any) -> Any:
        """Run one task through the graph, namespaced by tenant and instance."""
        from agent_forge.core.checkpointer import namespaced_thread_id
        from agent_forge.core.state import AgentState

        raw = await graph.ainvoke(
            state,
            {
                "configurable": {
                    "thread_id": namespaced_thread_id(
                        state.identity.tenant_id, settings.instance, state.thread_id
                    )
                }
            },
        )
        if isinstance(raw, AgentState):
            return raw
        payload = {k: v for k, v in raw.items() if not k.startswith("__")}
        final = AgentState.model_validate(payload)
        # An interrupt means the graph paused for a human; upstream must see that as
        # `input-required`, not as a task that quietly finished.
        if raw.get("__interrupt__"):
            return final.model_copy(update={"status": "awaiting_approval"})
        return final

    tasks = TaskRunner(
        invoke,
        agent_name=profile.identity.agent_name,
        area=profile.identity.area,
    )

    aggregator = Aggregator(
        bus=bus,
        source=profile.identity.agent_name,
        tenant_id=profile.identity.tenant_id,
        resume=_resume_hook(graph, tasks, settings.instance),
        ledger=ledger,
    )

    runtime = Runtime(
        settings=settings,
        profile=profile,
        prompts=prompts,
        policy=policy,
        gateway=gateway,
        deps=deps,
        graph=graph,
        authenticator=Authenticator(AuthSettings.from_env(source)),
        memory=memory,
        knowledge=knowledge,
        connectors=connectors,
        tasks=tasks,
        governance=governance,
        bus=bus,
        ledger=ledger,
        aggregator=aggregator,
        channel_settings=ChannelSettings.from_env(dict(source)),
        approvals=InMemoryApprovalStore(),
        approval_policy=ApprovalPolicy(
            approvers_group=profile.governance.hitl_approvers_group,
            elevated_group=profile.governance.hitl_elevated_group,
        ),
        capabilities=capabilities,
    )
    stack.push_async_callback(runtime.aclose)

    log.info(
        "runtime.ready",
        instance=runtime.instance_key,
        subgraph=subgraph.name,
        checkpointer=settings.checkpointer,
        channels=profile.channels.enabled_names(),
        capabilities=sorted(k for k, v in capabilities.items() if v),
        memory_scope=profile.memory.ltm.scope,
        knowledge=profile.knowledge.rag.enabled,
        connectors=connectors.names(),
    )
    return runtime
