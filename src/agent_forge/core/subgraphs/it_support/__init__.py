"""IT support subgraph: a worked example of what specialisation buys you.

Three things a generalist cannot do, all of them domain knowledge and none of them in
the core:

1. Recognise the shape of an IT request (incident, access request, how-to) and steer
   retrieval and tone accordingly.
2. Know that "reset a password" or "grant access" are *actions on a system of record*,
   not questions, and declare the action category so the autonomy map can require human
   approval for them.
3. Refuse to guess at an incident's cause when the runbook is not in the corpus, because
   in this domain a confident wrong answer costs an outage.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import ClassVar

from agent_forge.core.state import PlanStep
from agent_forge.core.subgraphs.base import (
    DomainContext,
    DomainOutcome,
    DomainSubgraph,
    Finding,
    ToolRequest,
)
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

# Deliberately literal and inspectable rather than an LLM classifier: intent detection
# that gates human approval must be reviewable by the people who own the process.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "incident",
        re.compile(
            r"\b(no funciona|caido|caída|error|falla|fallo|incidencia|lento|"
            r"not working|down|outage|broken)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "access_request",
        re.compile(
            r"\b(acceso|permiso|dar de alta|habilitar|desbloquear|restablecer|"
            r"resetear|reset|access|permission|unlock)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "how_to",
        re.compile(
            r"\b(como|cómo|how do i|how to|donde|dónde|configurar|instalar)\b", re.IGNORECASE
        ),
    ),
)

# Intents that change a system of record. The names are the action categories an
# operator writes in `autonomy.overrides`, which is how a profile raises them to A2/A3.
_ACTION_CATEGORY: dict[str, str] = {
    "access_request": "gestionar_accesos",
    "incident": "gestionar_incidencia",
}


class ItSupportSubgraph(DomainSubgraph):
    """Domain subgraph for an IT service desk."""

    name: ClassVar[str] = "it_support"
    description: ClassVar[str] = (
        "IT service desk: incidents, access requests and how-to questions, with "
        "ticketing actions gated by the autonomy map."
    )
    default_intents: ClassVar[tuple[str, ...]] = (
        "incident",
        "access_request",
        "how_to",
        "question",
    )

    def detect_intent(self, text: str) -> str:
        """First matching pattern wins; order encodes priority."""
        for intent, pattern in _PATTERNS:
            if pattern.search(text):
                return intent
        return "question"

    def system_guidance(self, ctx: DomainContext) -> str:
        intent = self.detect_intent(ctx.request)
        if intent == "incident":
            return (
                "Se trata de una incidencia. Pide el identificador del equipo o servicio "
                "si no lo tienes. No propongas una causa raiz sin respaldo documental: "
                "en este dominio una hipotesis presentada como hecho alarga la caida."
            )
        if intent == "access_request":
            return (
                "Se trata de una solicitud de acceso. Indica siempre que requiere "
                "aprobacion y quien aprueba. No afirmes que el acceso quedo concedido."
            )
        return ""

    async def refine_plan(self, ctx: DomainContext, plan: Sequence[PlanStep]) -> list[PlanStep]:
        """Tag steps with the action category so the autonomy map can find them."""
        intent = self.detect_intent(ctx.request)
        category = _ACTION_CATEGORY.get(intent)
        if category is None:
            return list(plan)
        return [
            step.model_copy(update={"action_category": step.action_category or category})
            for step in plan
        ]

    async def gather(self, ctx: DomainContext) -> DomainOutcome:
        intent = self.detect_intent(ctx.request)
        outcome = DomainOutcome(guidance=self.system_guidance(ctx))
        outcome.scratchpad["it_support_intent"] = intent

        if ctx.retrieve is not None and ctx.request:
            # Bias retrieval towards runbooks for incidents; for everything else the
            # plain question retrieves better than a decorated one.
            query = f"runbook {ctx.request}" if intent == "incident" else ctx.request
            for result in await ctx.retrieve(query):
                outcome.findings.append(
                    Finding(
                        source=result.citation.reference,
                        content=result.text,
                        classification=result.citation.classification,
                        citation=result.citation,
                    )
                )
                outcome.citations.append(result.citation)

        category = _ACTION_CATEGORY.get(intent)
        if category is not None:
            ticket_tool = next((t for t in ctx.tools if t.name.endswith(".create_ticket")), None)
            if ticket_tool is not None:
                # Requesting is not executing: the core still checks the allowlist and
                # the autonomy map, and pauses for approval when the level demands it.
                outcome.tool_requests.append(
                    ToolRequest(
                        tool=ticket_tool.name,
                        args={"summary": ctx.request[:200], "category": intent},
                        action_category=category,
                        reason=f"intent {intent} requires a tracked ticket",
                    )
                )

        log.debug(
            "subgraph.gathered",
            subgraph=self.name,
            intent=intent,
            findings=len(outcome.findings),
            tool_requests=len(outcome.tool_requests),
        )
        return outcome
