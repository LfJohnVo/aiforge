"""Test doubles that satisfy the same Protocols as the production implementations.

These are the "development" half of the two-implementation rule in
``docs/CODING_STANDARDS.md``: every boundary has a real adapter and one of these, and
both are held to the same contract tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_forge.core.autonomy import AutonomyLevel, AutonomyMap
from agent_forge.core.classification import Classification
from agent_forge.core.graph import GraphDeps
from agent_forge.core.planner import Planner
from agent_forge.core.prompts import PromptRegistry
from agent_forge.core.router import IntentRouter
from agent_forge.core.state import AgentState, Citation, Identity, Message
from agent_forge.core.subgraphs.base import ToolDescriptor
from agent_forge.core.subgraphs.generalist import GeneralistSubgraph
from agent_forge.gateway.litellm_client import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    GovernedGateway,
)
from agent_forge.gateway.model_policy import ModelBackend, ModelPolicy, Sovereignty

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "configs" / "prompts"

LOCAL_FAST = ModelBackend("local/fast", Sovereignty.LOCAL, Classification.C4)
LOCAL_QUALITY = ModelBackend("local/quality", Sovereignty.LOCAL, Classification.C4)
EXTERNAL = ModelBackend("anthropic/claude", Sovereignty.EXTERNAL, Classification.C2)


@dataclass
class FakeTransport:
    """Records what it was asked to send and replies with canned text."""

    replies: list[str] = field(default_factory=lambda: ["respuesta de prueba"])
    calls: list[ChatRequest] = field(default_factory=list)
    healthy: bool = True
    closed: bool = False

    def _next_reply(self) -> str:
        if len(self.replies) > 1:
            return self.replies.pop(0)
        return self.replies[0] if self.replies else ""

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls.append(request)
        return ChatResponse(
            content=self._next_reply(),
            model=request.model,
            tokens_in=17,
            tokens_out=5,
            cost_usd=0.0001,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatChunk]:
        self.calls.append(request)
        for word in self._next_reply().split(" "):
            yield ChatChunk(delta=word + " ")
        yield ChatChunk(finish_reason="stop")

    async def health(self) -> bool:
        return self.healthy

    async def aclose(self) -> None:
        self.closed = True

    @property
    def models_used(self) -> list[str]:
        return [call.model for call in self.calls]


@dataclass(frozen=True, slots=True)
class FakeRetrievalResult:
    """Shape the knowledge layer will return in F3."""

    text: str
    citation: Citation


def fake_retriever(
    results: Sequence[FakeRetrievalResult],
) -> Any:
    """A retrieve callable returning fixed results, ignoring the query."""

    async def retrieve(query: str) -> list[FakeRetrievalResult]:
        del query
        return list(results)

    return retrieve


def make_policy() -> ModelPolicy:
    return ModelPolicy([LOCAL_FAST, LOCAL_QUALITY, EXTERNAL])


def make_gateway(
    transport: FakeTransport | None = None,
    *,
    external_allowed: Sequence[str] = ("anthropic/claude",),
) -> GovernedGateway:
    return GovernedGateway(
        transport or FakeTransport(), make_policy(), external_allowed=external_allowed
    )


def make_prompts() -> PromptRegistry:
    return PromptRegistry.from_directory(PROMPTS_DIR)


def make_deps(
    *,
    gateway: GovernedGateway | None = None,
    subgraph: Any | None = None,
    autonomy_map: AutonomyMap | None = None,
    tool_catalog: Sequence[ToolDescriptor] = (),
    tool_executor: Any | None = None,
    retrieve: Any | None = None,
    governance: Any | None = None,
    quality: Any | None = None,
    max_retries: int = 2,
) -> GraphDeps:
    prompts = make_prompts()
    gw = gateway if gateway is not None else make_gateway()
    sub = subgraph or GeneralistSubgraph()
    deps = GraphDeps(
        subgraph=sub,
        prompts=prompts,
        planner=Planner(prompts, gateway=None),  # no planner model call in unit tests
        router=IntentRouter(sub.intents),
        gateway=gw,
        autonomy_map=autonomy_map or AutonomyMap(default=AutonomyLevel.A1),
        tool_catalog=tool_catalog,
        tool_executor=tool_executor,
        retrieve=retrieve,
        quality=quality,
        persona="Asistente de pruebas del area.",
        language="es",
        max_retries=max_retries,
    )
    if governance is not None:
        deps.governance = governance
    return deps


def make_state(
    text: str = "hola",
    *,
    tenant_id: str = "acme-mx",
    user_id: str | None = "u1",
    groups: Sequence[str] = ("finanzas",),
    ceiling: Classification = Classification.C2,
    authenticated: bool = True,
    area: str = "finanzas",
) -> AgentState:
    return AgentState(
        identity=Identity(
            tenant_id=tenant_id,
            user_id=user_id,
            groups=tuple(groups),
            classification_ceiling=ceiling,
            authenticated=authenticated,
        ),
        agent_name="asistente-pruebas",
        area=area,
        trace_id="00-trace-span-01",
        messages=[Message(role="user", content=text)],
    )
