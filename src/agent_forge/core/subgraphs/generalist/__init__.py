"""Generalist domain subgraph: the default, deliberately domain-free.

It does the one thing every area needs -- retrieve what the requester is allowed to see
and hand it to synthesis -- and nothing else. A real area either configures this one or
ships its own plugin; see ``it_support`` for what specialisation looks like.
"""

from __future__ import annotations

from typing import ClassVar

from agent_forge.core.subgraphs.base import (
    DomainContext,
    DomainOutcome,
    DomainSubgraph,
    Finding,
)
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)


class GeneralistSubgraph(DomainSubgraph):
    """Retrieval plus synthesis, with no domain assumptions."""

    name: ClassVar[str] = "generalist"
    description: ClassVar[str] = (
        "Answers from the area's documentation with citations; no domain-specific logic."
    )
    default_intents: ClassVar[tuple[str, ...]] = ("question", "summary", "lookup")

    async def gather(self, ctx: DomainContext) -> DomainOutcome:
        outcome = DomainOutcome()
        if ctx.retrieve is None or not ctx.request:
            # Knowledge disabled or nothing to look up: the model answers from the
            # conversation alone, and synthesis will say so explicitly.
            return outcome

        results = await ctx.retrieve(ctx.request)
        for result in results:
            outcome.findings.append(
                Finding(
                    source=result.citation.reference,
                    content=result.text,
                    classification=result.citation.classification,
                    citation=result.citation,
                )
            )
            outcome.citations.append(result.citation)

        log.debug(
            "subgraph.gathered",
            subgraph=self.name,
            findings=len(outcome.findings),
            classification=str(outcome.classification()),
        )
        return outcome
