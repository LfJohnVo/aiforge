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
from agent_forge.core.checkpointer import CheckpointerKind, open_checkpointer
from agent_forge.core.errors import CapabilityUnavailableError, ProfileError
from agent_forge.core.graph import GraphDeps, build_graph
from agent_forge.core.hitl import ApprovalPolicy, ApprovalStore, InMemoryApprovalStore
from agent_forge.core.planner import Planner
from agent_forge.core.prompts import PromptRegistry
from agent_forge.core.router import IntentRouter
from agent_forge.core.subgraphs.base import load_subgraph
from agent_forge.gateway.litellm_client import GovernedGateway, LiteLLMTransport
from agent_forge.gateway.model_policy import ModelPolicy
from agent_forge.knowledge import KnowledgeService, build_knowledge
from agent_forge.memory import MemoryManager, Scrubber, build_memory
from agent_forge.observability.logging import configure_logging, get_logger
from agent_forge.profile import AgentProfile, load_profile

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
        )

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


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
    )

    checkpointer = await stack.enter_async_context(
        open_checkpointer(
            settings.checkpointer,
            postgres_dsn=settings.postgres_dsn,
            redis_url=settings.redis_url,
        )
    )
    graph = build_graph(deps, checkpointer=checkpointer)

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
    )
    return runtime
