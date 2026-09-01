"""The domain subgraph contract.

A subgraph is where *all* business-domain knowledge lives. The core graph
(``core/graph.py``) calls the three hooks below and knows nothing else about the domain;
adding an area never touches the core.

Deliberately, this module does **not** import LangGraph. Subgraphs see a small facade
(``DomainContext`` / ``DomainOutcome``), which is the mitigation recorded in ADR-001 for
coupling the product to one agentic framework: replacing the runtime would rewrite
``core/graph.py`` and leave every domain plugin untouched.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, ClassVar

from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import Classification
from agent_forge.core.errors import AgentForgeError
from agent_forge.core.prompts import PromptRegistry
from agent_forge.core.state import AgentState, Citation, PlanStep

ENTRY_POINT_GROUP = "agent_forge.subgraphs"


class SubgraphError(AgentForgeError):
    """The requested domain subgraph is unknown or failed to load."""

    code = "subgraph_unavailable"


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """What a subgraph is allowed to know about a tool.

    Only what it needs to choose one: no credentials, no endpoints, no transport.
    """

    name: str
    description: str
    autonomy_min: AutonomyLevel = AutonomyLevel.A0
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Finding:
    """A piece of gathered material, ready to hand to synthesis."""

    source: str
    content: str
    classification: Classification = Classification.C0
    citation: Citation | None = None


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """A tool the subgraph wants executed. The core decides whether it may run."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    action_category: str | None = None
    reason: str = ""


@dataclass(slots=True)
class DomainContext:
    """Everything a subgraph is given. Read-only with respect to the graph state."""

    state: AgentState
    prompts: PromptRegistry
    tools: Sequence[ToolDescriptor] = ()
    # Set by the core when the knowledge layer is available (F3). A subgraph must cope
    # with it being None: a cell can run with knowledge disabled.
    retrieve: Any | None = None
    profile_extras: dict[str, Any] = field(default_factory=dict)

    @property
    def request(self) -> str:
        return self.state.last_user_message

    @property
    def area(self) -> str:
        return self.state.area

    @property
    def language(self) -> str:
        lang = self.profile_extras.get("language")
        return str(lang) if lang else "es"

    def tool(self, name: str) -> ToolDescriptor | None:
        return next((t for t in self.tools if t.name == name), None)


@dataclass(slots=True)
class DomainOutcome:
    """What a subgraph produces. Never mutates state directly."""

    findings: list[Finding] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    tool_requests: list[ToolRequest] = field(default_factory=list)
    scratchpad: dict[str, Any] = field(default_factory=dict)
    # Extra guidance appended to the system prompt for this turn only.
    guidance: str = ""

    def classification(self) -> Classification:
        """Highest classification introduced by this subgraph's material."""
        levels = [f.classification for f in self.findings]
        levels.extend(c.classification for c in self.citations)
        return max(levels) if levels else Classification.C0


class DomainSubgraph(abc.ABC):
    """Base class for every domain plugin.

    Register with an entry point:

        [project.entry-points."agent_forge.subgraphs"]
        finance = "my_package.finance:FinanceSubgraph"
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    default_intents: ClassVar[tuple[str, ...]] = ()

    def __init__(self, extra_intents: Sequence[str] = ()) -> None:
        if not self.name:
            raise SubgraphError("subgraph class must declare a name", cls=type(self).__name__)
        self._intents = tuple(dict.fromkeys([*self.default_intents, *extra_intents]))

    @property
    def intents(self) -> tuple[str, ...]:
        """Intent labels this subgraph handles, for the router."""
        return self._intents

    # ------------------------------------------------------------------ hooks

    def system_guidance(self, ctx: DomainContext) -> str:
        """Domain-specific text appended to the system prompt. Default: nothing."""
        del ctx
        return ""

    async def refine_plan(self, ctx: DomainContext, plan: Sequence[PlanStep]) -> list[PlanStep]:
        """Adjust the generic plan with domain knowledge. Default: accept it."""
        del ctx
        return list(plan)

    @abc.abstractmethod
    async def gather(self, ctx: DomainContext) -> DomainOutcome:
        """Collect the material needed to answer, and request any tools.

        This is the one hook a domain must implement. It runs after the planner and
        before tool execution; anything it returns flows into synthesis.
        """

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} intents={self._intents}>"


class SubgraphRegistry:
    """Discovers subgraph plugins through entry points."""

    def __init__(self, group: str = ENTRY_POINT_GROUP) -> None:
        self._group = group
        self._classes: dict[str, type[DomainSubgraph]] | None = None

    def _discover(self) -> dict[str, type[DomainSubgraph]]:
        if self._classes is not None:
            return self._classes
        found: dict[str, type[DomainSubgraph]] = {}
        for entry in entry_points(group=self._group):
            try:
                loaded = entry.load()
            except Exception as exc:  # a broken plugin must fail loudly at boot
                raise SubgraphError(
                    "failed to load subgraph plugin", plugin=entry.name, detail=str(exc)
                ) from exc
            if not (isinstance(loaded, type) and issubclass(loaded, DomainSubgraph)):
                raise SubgraphError(
                    "entry point does not point at a DomainSubgraph subclass",
                    plugin=entry.name,
                    got=repr(loaded),
                )
            found[entry.name] = loaded
        self._classes = found
        return found

    def available(self) -> list[str]:
        return sorted(self._discover())

    def create(self, name: str, extra_intents: Sequence[str] = ()) -> DomainSubgraph:
        classes = self._discover()
        cls = classes.get(name)
        if cls is None:
            raise SubgraphError(
                "unknown domain subgraph", requested=name, available=sorted(classes)
            )
        return cls(extra_intents=extra_intents)


_REGISTRY = SubgraphRegistry()


def load_subgraph(name: str, extra_intents: Sequence[str] = ()) -> DomainSubgraph:
    """Instantiate a registered subgraph by profile name."""
    return _REGISTRY.create(name, extra_intents)


def available_subgraphs() -> list[str]:
    return _REGISTRY.available()
