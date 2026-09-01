"""C0-C4 classification at ingestion time.

Classification happens once, when a document is ingested, and the label travels with
every chunk. Doing it at query time would mean the same document could be labelled
differently on two requests, and the label is what the model-routing guarantee rests on.

Two tiers, and an override that beats both:

1. **Rules** -- markers in the text, the source's own sensitivity label, the path. Cheap,
   deterministic, auditable, and right for most corpora.
2. **A local model** -- only for what the rules cannot decide, and only ever local: the
   document is exactly the thing whose sensitivity is unknown, so it cannot be sent out
   to find out.

The direction of error matters. Classifying too high costs a local model call;
classifying too low is a leak. Everything here is biased upward.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_forge.core.classification import Classification
from agent_forge.core.errors import ModelGatewayError
from agent_forge.gateway.litellm_client import GovernedGateway
from agent_forge.knowledge.documents import Document
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

# Ordered high to low: the first match wins, so a document mentioning both "secreto" and
# "publico" is treated as secret.
_MARKERS: tuple[tuple[Classification, re.Pattern[str]], ...] = (
    (
        Classification.C4,
        re.compile(
            r"\b(secreto|top ?secret|maxima reserva|strictly confidential|"
            r"clasificacion:\s*c4)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Classification.C3,
        re.compile(
            r"\b(restringido|restricted|uso exclusivo|solo direccion|need.to.know|"
            r"clasificacion:\s*c3)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Classification.C2,
        re.compile(
            r"\b(confidencial|confidential|uso interno exclusivo|propietario|"
            r"clasificacion:\s*c2)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Classification.C1,
        re.compile(
            r"\b(uso interno|internal use|solo para empleados|clasificacion:\s*c1)\b",
            re.IGNORECASE,
        ),
    ),
    (
        Classification.C0,
        re.compile(
            r"\b(publico|public release|difusion libre|clasificacion:\s*c0)\b", re.IGNORECASE
        ),
    ),
)

# Sensitivity labels as SharePoint and similar systems report them.
_SOURCE_LABELS: dict[str, Classification] = {
    "public": Classification.C0,
    "publico": Classification.C0,
    "general": Classification.C1,
    "internal": Classification.C1,
    "interno": Classification.C1,
    "confidential": Classification.C2,
    "confidencial": Classification.C2,
    "highly confidential": Classification.C3,
    "restricted": Classification.C3,
    "restringido": Classification.C3,
    "secret": Classification.C4,
    "secreto": Classification.C4,
}

# Only the first part of a document is scanned for markers: a classification banner is at
# the top, and scanning megabytes for it is waste.
HEADER_CHARS = 4000


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """The label plus why, so an auditor can reconstruct the decision."""

    classification: Classification
    method: str  # override | source_label | marker | model | default
    reason: str = ""
    confidence: float = 1.0


@dataclass(slots=True)
class Classifier:
    """Assigns C0-C4 to a document at ingestion time."""

    default: Classification = Classification.C2
    gateway: GovernedGateway | None = None
    model_alias: str = "local/fast"
    use_model: bool = True
    # Manual overrides keyed by source_id. Wins over everything, including a marker in
    # the text: a human decision about a specific document is the highest authority.
    overrides: dict[str, Classification] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.default == Classification.C0:
            raise ValueError(
                "the ingestion default must not be C0: content whose sensitivity could "
                "not be determined must never default to public"
            )

    async def classify(self, document: Document) -> ClassificationResult:
        """Label a document, trying each tier in order of authority."""
        override = self.overrides.get(document.source_id)
        if override is not None:
            return ClassificationResult(override, "override", "manual override")

        labelled = self._from_source_label(document)
        if labelled is not None:
            return labelled

        marked = self.from_markers(document.text)
        if marked is not None:
            return marked

        model = await self._from_model(document)
        if model is not None:
            return model

        return ClassificationResult(self.default, "default", "no marker, label or model verdict")

    def _from_source_label(self, document: Document) -> ClassificationResult | None:
        raw = document.metadata.get("sensitivity_label")
        if not isinstance(raw, str):
            return None
        found = _SOURCE_LABELS.get(raw.strip().lower())
        if found is None:
            log.info("classifier.unknown_source_label", label=raw[:40])
            return None
        return ClassificationResult(found, "source_label", f"source label {raw!r}")

    def from_markers(self, text: str) -> ClassificationResult | None:
        """Scan the header for an explicit classification marker."""
        header = text[:HEADER_CHARS]
        for level, pattern in _MARKERS:
            match = pattern.search(header)
            if match is not None:
                return ClassificationResult(level, "marker", f"marker {match.group(0)!r} in header")
        return None

    async def _from_model(self, document: Document) -> ClassificationResult | None:
        """Ask the local model. Any failure falls through to the default, never down."""
        if not self.use_model or self.gateway is None:
            return None
        excerpt = document.text[:HEADER_CHARS]
        try:
            response = await self.gateway.complete(
                [
                    {"role": "system", "content": _MODEL_PROMPT},
                    {
                        "role": "user",
                        "content": f"Titulo: {document.title}\n\nExtracto:\n{excerpt}",
                    },
                ],
                # The document's sensitivity is precisely what is unknown, so it is
                # treated as maximal: only a sovereign backend may see it.
                classification=Classification.C4,
                preferred=[self.model_alias],
                tenant_id=document.tenant_id,
                temperature=0.0,
                max_tokens=8,
            )
        except ModelGatewayError as exc:
            log.warning("classifier.model_unavailable", detail=str(exc))
            return None

        parsed = _parse_level(response.content)
        if parsed is None:
            log.info("classifier.model_returned_unparseable", got=response.content[:40])
            return None
        # Never below the profile default: a model that says "public" about a document
        # nobody labelled is not evidence enough to publish it.
        level = max(parsed, self.default)
        return ClassificationResult(level, "model", f"local model said {parsed}", confidence=0.7)


_MODEL_PROMPT = (
    "Clasifica la sensibilidad del documento y responde SOLO con una etiqueta: "
    "C0 (publico), C1 (interno), C2 (confidencial), C3 (restringido) o C4 (secreto). "
    "Ante la duda, elige el nivel mas alto de los dos que consideres."
)

_LEVEL = re.compile(r"\bC([0-4])\b", re.IGNORECASE)


def _parse_level(text: str) -> Classification | None:
    match = _LEVEL.search(text.strip())
    return Classification(int(match.group(1))) if match else None


def parse_overrides(raw: Sequence[dict[str, Any]] | None) -> dict[str, Classification]:
    """Read manual overrides from configuration: ``[{source_id, classification}]``."""
    out: dict[str, Classification] = {}
    for entry in raw or []:
        source = entry.get("source_id")
        level = entry.get("classification")
        if isinstance(source, str) and level is not None:
            out[source] = Classification.parse(level)
    return out
