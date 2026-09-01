"""Typed model of ``agent.profile.yaml``.

This file is the contract of the whole product: installing the cell somewhere else means
editing the YAML this module validates, and nothing else. Validation happens at startup
and is deliberately strict -- ``extra="forbid"`` everywhere -- because a silently ignored
typo in a profile is a misconfigured production agent.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_forge.core.autonomy import AutonomyLevel, AutonomyMap
from agent_forge.core.classification import Classification

SCHEMA_VERSION = 1

# Used in Redis keys, NATS subjects, Qdrant payloads, container and volume names.
# Hyphens only: these values end up where underscores are not portable (DNS labels).
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")
Slug = Annotated[str, Field(pattern=SLUG.pattern, min_length=2, max_length=64)]

# Connector and database aliases become tool-name prefixes (`erp_ro.query`), never
# hostnames, so underscores are allowed and idiomatic there.
ALIAS = re.compile(r"^[a-z][a-z0-9_]{0,62}[a-z0-9]$")
Alias = Annotated[str, Field(pattern=ALIAS.pattern, min_length=2, max_length=64)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# --------------------------------------------------------------------- identity


class Identity(_Base):
    """Who this cell is. Determines every namespace it touches."""

    agent_name: Slug
    tenant_id: Slug
    area: Slug
    language: str = Field(default="es", min_length=2, max_length=8)
    persona: str = Field(min_length=10, max_length=4000)


class Domain(_Base):
    """Which domain subgraph plugin to load."""

    subgraph: str = Field(default="generalist", min_length=1)
    intents_extra: list[str] = Field(default_factory=list)


class Autonomy(_Base):
    """Default autonomy plus per-action-category overrides."""

    default: AutonomyLevel = AutonomyLevel.A1
    overrides: dict[str, AutonomyLevel] = Field(default_factory=dict)

    def to_map(self) -> AutonomyMap:
        return AutonomyMap(default=self.default, overrides=dict(self.overrides))


# --------------------------------------------------------------------- channels


class ChannelToggle(_Base):
    enabled: bool = False


class Channels(_Base):
    openai_api: ChannelToggle = ChannelToggle(enabled=True)
    openwebui: ChannelToggle = ChannelToggle(enabled=False)
    copilot_studio: ChannelToggle = ChannelToggle(enabled=False)
    teams: ChannelToggle = ChannelToggle(enabled=False)
    slack: ChannelToggle = ChannelToggle(enabled=False)
    websocket: ChannelToggle = ChannelToggle(enabled=False)

    def enabled_names(self) -> list[str]:
        return [name for name in type(self).model_fields if getattr(self, name).enabled]


class Upstream(_Base):
    """How a higher-level orchestrator reaches this cell."""

    mcp_server: ChannelToggle = ChannelToggle(enabled=True)
    a2a: ChannelToggle = ChannelToggle(enabled=True)
    openapi: ChannelToggle = ChannelToggle(enabled=True)
    orchestrator: Literal["standalone", "copilot_studio", "bedrock", "vllm_supervisor"] = (
        "standalone"
    )


# -------------------------------------------------------------------- knowledge


class SharePointSource(_Base):
    type: Literal["sharepoint"]
    site: str = Field(min_length=8)
    drives: list[str] = Field(min_length=1)
    sync_cron: str | None = None
    default_acl_groups: list[str] = Field(default_factory=list)
    default_classification: Classification | None = None


class FolderSource(_Base):
    type: Literal["folder"]
    path: str = Field(min_length=1)
    sync_cron: str | None = None
    default_acl_groups: list[str] = Field(default_factory=list)
    default_classification: Classification | None = None


class S3Source(_Base):
    type: Literal["s3"]
    bucket: str = Field(min_length=1)
    prefix: str = ""
    sync_cron: str | None = None
    default_acl_groups: list[str] = Field(default_factory=list)
    default_classification: Classification | None = None


KnowledgeSource = Annotated[SharePointSource | FolderSource | S3Source, Field(discriminator="type")]


class RagSettings(_Base):
    enabled: bool = True
    top_k: int = Field(default=8, ge=1, le=100)
    rerank: bool = True
    hybrid: bool = True


class GraphRagSettings(_Base):
    enabled: bool = False


class CagSettings(_Base):
    enabled: bool = False
    stable_corpus: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _corpus_required_when_enabled(self) -> CagSettings:
        if self.enabled and not self.stable_corpus:
            raise ValueError("cag.enabled requires a non-empty cag.stable_corpus")
        return self


class Knowledge(_Base):
    sources: list[KnowledgeSource] = Field(default_factory=list)
    rag: RagSettings = RagSettings()
    graphrag: GraphRagSettings = GraphRagSettings()
    cag: CagSettings = CagSettings()
    # Never C0: classifying downwards is the only direction that leaks.
    default_classification: Classification = Classification.C2

    @model_validator(mode="after")
    def _default_classification_is_not_public(self) -> Knowledge:
        if self.default_classification == Classification.C0:
            raise ValueError(
                "knowledge.default_classification must not be C0: content whose "
                "sensitivity could not be determined must not default to public"
            )
        return self


# ----------------------------------------------------------------------- models


class Budgets(_Base):
    monthly_usd: float = Field(default=0.0, ge=0)


class Models(_Base):
    """Model aliases, resolved in configs/litellm.yaml.

    The profile never names a concrete model: swapping model family must not touch a
    single profile.
    """

    fast: str = Field(default="local/fast", min_length=1)
    quality: str = Field(default="local/quality", min_length=1)
    external_allowed: list[str] = Field(default_factory=list)
    budgets: Budgets = Budgets()


# ----------------------------------------------------------------------- memory


class LtmSettings(_Base):
    enabled: bool = True
    scope: Literal["user", "area", "tenant"] = "area"


class EpisodicSettings(_Base):
    enabled: bool = True


class SemanticCacheSettings(_Base):
    enabled: bool = True
    similarity: float = Field(default=0.92, ge=0.5, le=1.0)
    ttl_hours: int = Field(default=72, ge=1)


class Memory(_Base):
    stm_ttl_minutes: int = Field(default=240, ge=1)
    ltm: LtmSettings = LtmSettings()
    episodic: EpisodicSettings = EpisodicSettings()
    semantic_cache: SemanticCacheSettings = SemanticCacheSettings()
    retention_days: int = Field(default=365, ge=1)


# ------------------------------------------------------------------- connectors


class McpGatewaySettings(_Base):
    url: str = ""
    allowlist: list[str] = Field(default_factory=list)


class N8nSettings(_Base):
    url: str = ""
    workflows: list[str] = Field(default_factory=list)


class DatabaseConnector(_Base):
    type: Literal["postgres", "mysql", "mongodb", "redis"]
    alias: Alias
    dsn_env: str = Field(min_length=1)
    readonly: bool = True


class Connectors(_Base):
    mcp_gateway: McpGatewaySettings = McpGatewaySettings()
    n8n: N8nSettings = N8nSettings()
    databases: list[DatabaseConnector] = Field(default_factory=list)

    @model_validator(mode="after")
    def _aliases_are_unique(self) -> Connectors:
        aliases = [db.alias for db in self.databases]
        duplicates = {a for a in aliases if aliases.count(a) > 1}
        if duplicates:
            raise ValueError(f"duplicate database aliases: {sorted(duplicates)}")
        return self


# ------------------------------------------------------------------- governance


class DlpSettings(_Base):
    enabled: bool = True
    rules_file: str = "configs/policies/dlp_rules.yaml"


class Governance(_Base):
    pdp_url: str = ""
    fail_mode: Literal["closed", "permissive_c0c1"] = "closed"
    hitl_approvers_group: str = ""
    hitl_elevated_group: str = ""
    dlp: DlpSettings = DlpSettings()

    @model_validator(mode="after")
    def _approvers_required_when_hitl_possible(self) -> Governance:
        # Enforced against the autonomy map at profile level, where both are visible.
        return self


# ----------------------------------------------------------------------- events


class AggregatorSettings(_Base):
    enabled: bool = True


class JudgeThresholds(_Base):
    groundedness: float = Field(default=0.85, ge=0.0, le=1.0)
    safety: float = Field(default=0.95, ge=0.0, le=1.0)


class JudgeSettings(_Base):
    mode: Literal["platform", "local"] = "local"
    thresholds: JudgeThresholds = JudgeThresholds()
    max_retries: int = Field(default=2, ge=0, le=10)


class Events(_Base):
    fabric_url: str = ""
    aggregator: AggregatorSettings = AggregatorSettings()
    judge: JudgeSettings = JudgeSettings()


# ---------------------------------------------------------------- observability


class LangfuseSettings(_Base):
    enabled: bool = False


class MetricsSettings(_Base):
    enabled: bool = True


class Observability(_Base):
    otel_endpoint: str = ""
    langfuse: LangfuseSettings = LangfuseSettings()
    metrics: MetricsSettings = MetricsSettings()


class Analytics(_Base):
    """RF-14, off by default: only meaningful for domains with telemetry."""

    enabled: bool = False


# ---------------------------------------------------------------------- profile


class AgentProfile(_Base):
    """The whole profile. One instance of this object is one Agent Cell."""

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    identity: Identity
    domain: Domain = Domain()
    autonomy: Autonomy = Autonomy()
    channels: Channels = Channels()
    upstream: Upstream = Upstream()
    knowledge: Knowledge = Knowledge()
    models: Models = Models()
    memory: Memory = Memory()
    connectors: Connectors = Connectors()
    governance: Governance = Governance()
    events: Events = Events()
    observability: Observability = Observability()
    analytics: Analytics = Analytics()

    @model_validator(mode="after")
    def _schema_version_supported(self) -> AgentProfile:
        if self.schema_version > SCHEMA_VERSION:
            raise ValueError(
                f"profile schema_version {self.schema_version} is newer than this build "
                f"supports ({SCHEMA_VERSION}); upgrade agent-forge"
            )
        return self

    @model_validator(mode="after")
    def _hitl_needs_approvers(self) -> AgentProfile:
        """A profile that can pause for approval must say who may approve."""
        levels = [self.autonomy.default, *self.autonomy.overrides.values()]
        if any(level.requires_approval for level in levels) and not (
            self.governance.hitl_approvers_group
        ):
            raise ValueError(
                "autonomy allows A2+ actions but governance.hitl_approvers_group is "
                "empty: nobody could ever approve them and tasks would hang forever"
            )
        return self

    @model_validator(mode="after")
    def _cag_requires_rag_infrastructure(self) -> AgentProfile:
        if self.knowledge.cag.enabled and not self.knowledge.rag.enabled:
            raise ValueError("knowledge.cag requires knowledge.rag to be enabled")
        return self

    @model_validator(mode="after")
    def _some_way_in(self) -> AgentProfile:
        if not self.channels.enabled_names() and not (
            self.upstream.mcp_server.enabled
            or self.upstream.a2a.enabled
            or self.upstream.openapi.enabled
        ):
            raise ValueError(
                "no channel and no upstream surface is enabled: this cell would be unreachable"
            )
        return self

    # ------------------------------------------------------------ derived views

    @property
    def instance_key(self) -> str:
        """Stable identifier for this cell: ``tenant/agent``."""
        return f"{self.identity.tenant_id}/{self.identity.agent_name}"

    def autonomy_map(self) -> AutonomyMap:
        return self.autonomy.to_map()

    def required_capabilities(self) -> set[str]:
        """Optional extras this profile actually needs (checked at startup, ADR-005)."""
        needed: set[str] = set()
        if self.knowledge.rag.enabled or self.knowledge.graphrag.enabled:
            needed.add("knowledge")
        if any(
            source.type == "sharepoint" or source.type == "s3" for source in self.knowledge.sources
        ):
            needed.add("sources")
        if self.memory.ltm.enabled:
            needed.add("memory")
        if self.governance.dlp.enabled:
            needed.add("guardrails")
        if any(db.type in {"mysql", "mongodb"} for db in self.connectors.databases):
            needed.add("databases")
        if self.observability.langfuse.enabled:
            needed.add("evals")
        if self.analytics.enabled:
            needed.add("analytics")
        return needed
