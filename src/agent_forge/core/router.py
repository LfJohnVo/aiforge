"""Intent router.

Picks the intent label for a request so the planner and the subgraph can specialise.
The router is deliberately cheap and local: it runs on every message, it must not add
latency, and its decision is visible in the trace.

Two tiers, in order:

1. Lexical scoring against the intent vocabulary (subgraph defaults plus the profile's
   ``intents_extra``). Free, deterministic, and right most of the time.
2. A single small-model call, only when tier 1 finds nothing convincing and a gateway is
   available. Runs on ``models.fast`` and never on an external backend, because the raw
   user message is part of the task's classified material.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from agent_forge.core.classification import Classification
from agent_forge.core.errors import ModelGatewayError
from agent_forge.gateway.litellm_client import GovernedGateway
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

DEFAULT_INTENT = "question"
# Below this, lexical matching is guessing; ask the model instead.
CONFIDENCE_FLOOR = 0.34

_WORD = re.compile(r"[a-z0-9]+")


def normalise(text: str) -> list[str]:
    """Lowercase, strip accents, split into words.

    Accent folding matters: users type "como" and "cómo" interchangeably, and an intent
    router that treats them as different words is wrong half the time in Spanish.
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WORD.findall(stripped)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """The chosen intent, how sure we are, and how it was chosen."""

    intent: str
    confidence: float
    method: str  # lexical | model | default
    considered: tuple[str, ...] = ()


class IntentRouter:
    """Routes a request to one of the intents the loaded subgraph declares."""

    def __init__(
        self,
        intents: Sequence[str],
        *,
        gateway: GovernedGateway | None = None,
        fast_alias: str = "local/fast",
    ) -> None:
        self._intents = tuple(dict.fromkeys(intents)) or (DEFAULT_INTENT,)
        self._gateway = gateway
        self._fast_alias = fast_alias
        self._vocabulary = {
            intent: set(normalise(intent.replace("_", " "))) for intent in self._intents
        }

    @property
    def intents(self) -> tuple[str, ...]:
        return self._intents

    def route_lexical(self, text: str) -> RouteDecision:
        """Score each intent by word overlap with the request."""
        words = set(normalise(text))
        if not words:
            return RouteDecision(self._intents[0], 0.0, "default", self._intents)

        scores = {
            intent: len(vocab & words) / len(vocab)
            for intent, vocab in self._vocabulary.items()
            if vocab
        }
        best = max(scores, key=lambda k: scores[k], default=self._intents[0])
        confidence = scores.get(best, 0.0)
        if confidence <= 0:
            return RouteDecision(
                DEFAULT_INTENT if DEFAULT_INTENT in self._intents else self._intents[0],
                0.0,
                "default",
                self._intents,
            )
        return RouteDecision(best, round(confidence, 3), "lexical", self._intents)

    async def route(
        self,
        text: str,
        *,
        classification: Classification = Classification.C0,
        tenant_id: str = "",
        trace_id: str = "",
    ) -> RouteDecision:
        """Route, escalating to the small model only when lexical matching is unsure."""
        lexical = self.route_lexical(text)
        if lexical.confidence >= CONFIDENCE_FLOOR or self._gateway is None:
            return lexical

        try:
            response = await self._gateway.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "Classify the request into exactly one of these labels and "
                            "reply with the label alone, nothing else: " + ", ".join(self._intents)
                        ),
                    },
                    {"role": "user", "content": text[:2000]},
                ],
                classification=classification,
                preferred=[self._fast_alias],
                tenant_id=tenant_id,
                trace_id=trace_id,
                temperature=0.0,
                max_tokens=16,
            )
        except ModelGatewayError as exc:
            # The router is never worth failing a request over.
            log.warning("router.model_unavailable", detail=str(exc))
            return lexical

        candidate = response.content.strip().lower().replace(" ", "_")
        if candidate in self._intents:
            return RouteDecision(candidate, 0.9, "model", self._intents)

        log.debug("router.model_returned_unknown_label", label=candidate[:40])
        return lexical
