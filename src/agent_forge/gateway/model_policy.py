"""Model routing by data classification.

**This module is the only place in the codebase allowed to decide which model backend
serves a request.** Every other layer asks it. That is what makes the guarantee
"C3/C4 never reaches an external model" auditable instead of aspirational: there is one
function to read, one function to test, and no second path.

Two rules, in this order:

1. Sovereignty. Content at C3 or above may only go to a backend whose declared
   sovereignty is ``local``. This is not configurable and not overridable.
2. Preference. Among the backends that survive rule 1, pick what the profile asked for.

The classification used is always the task's **cumulative** maximum, never the
classification of the prompt alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from agent_forge.core.classification import (
    MAX_EXTERNAL_CLASSIFICATION,
    Classification,
)
from agent_forge.core.errors import ModelGatewayError, SovereigntyError


class Sovereignty(StrEnum):
    """Where a backend physically runs."""

    LOCAL = "local"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class ModelBackend:
    """A model alias as declared in ``configs/litellm.yaml``."""

    alias: str
    sovereignty: Sovereignty
    max_classification: Classification
    supports_tools: bool = True
    purpose: str = "chat"  # chat | embedding | rerank

    @property
    def is_local(self) -> bool:
        return self.sovereignty is Sovereignty.LOCAL

    def accepts(self, classification: Classification) -> bool:
        """Whether this backend may see content at that classification."""
        if classification.requires_sovereign_backend and not self.is_local:
            return False
        return classification <= self.max_classification


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """The chosen backend and why, so the reason can be traced and audited."""

    backend: ModelBackend
    classification: Classification
    reason: str
    rejected: tuple[str, ...] = ()

    @property
    def alias(self) -> str:
        return self.backend.alias


class ModelPolicy:
    """Decides the backend for a call, given the cumulative classification."""

    def __init__(self, backends: Iterable[ModelBackend]) -> None:
        self._backends: dict[str, ModelBackend] = {}
        for backend in backends:
            self._validate_declaration(backend)
            self._backends[backend.alias] = backend
        if not self._backends:
            raise ModelGatewayError("no model backends declared")

    @staticmethod
    def _validate_declaration(backend: ModelBackend) -> None:
        """Reject an impossible declaration at construction time, not at request time.

        An external backend claiming it may see C3 or C4 is a configuration error that
        must stop the process, because if it ever reached a routing decision the
        guarantee would already be broken.
        """
        if (
            backend.sovereignty is Sovereignty.EXTERNAL
            and backend.max_classification > MAX_EXTERNAL_CLASSIFICATION
        ):
            raise SovereigntyError(
                "external backend declares a classification ceiling above the maximum "
                "allowed for non-sovereign infrastructure",
                alias=backend.alias,
                declared=str(backend.max_classification),
                maximum=str(MAX_EXTERNAL_CLASSIFICATION),
            )

    # ---------------------------------------------------------------- queries

    def get(self, alias: str) -> ModelBackend:
        backend = self._backends.get(alias)
        if backend is None:
            raise ModelGatewayError("unknown model alias", alias=alias)
        return backend

    def aliases(self) -> Sequence[str]:
        return tuple(self._backends)

    def local_aliases(self, purpose: str = "chat") -> Sequence[str]:
        return tuple(
            alias
            for alias, backend in self._backends.items()
            if backend.is_local and backend.purpose == purpose
        )

    # ----------------------------------------------------------------- routing

    def route(
        self,
        classification: Classification,
        *,
        preferred: Sequence[str],
        external_allowed: Sequence[str] = (),
        purpose: str = "chat",
        require_tools: bool = False,
    ) -> RoutingDecision:
        """Pick a backend for content at ``classification``.

        ``preferred`` is the ordered wish list from the profile (``models.fast``,
        ``models.quality``). ``external_allowed`` is the profile's allowlist of governed
        external aliases; it can only ever *narrow* what rule 1 already permits.
        """
        rejected: list[str] = []
        allowed_external = set(external_allowed)

        for alias in preferred:
            backend = self._backends.get(alias)
            if backend is None:
                rejected.append(f"{alias}: not declared")
                continue
            if backend.purpose != purpose:
                rejected.append(f"{alias}: purpose {backend.purpose} != {purpose}")
                continue
            if require_tools and not backend.supports_tools:
                rejected.append(f"{alias}: no tool support")
                continue
            if not backend.accepts(classification):
                rejected.append(
                    f"{alias}: {backend.sovereignty} backend cannot serve {classification}"
                )
                continue
            if not backend.is_local and alias not in allowed_external:
                rejected.append(f"{alias}: external backend not in profile allowlist")
                continue
            return RoutingDecision(
                backend=backend,
                classification=classification,
                reason=f"preferred alias {alias} satisfies {classification}",
                rejected=tuple(rejected),
            )

        # Nothing preferred worked. Fall back to any *local* backend that fits: a
        # degraded answer from sovereign infrastructure beats both an error and,
        # emphatically, beats reaching for an external model.
        for alias, backend in self._backends.items():
            if (
                backend.is_local
                and backend.purpose == purpose
                and backend.accepts(classification)
                and (backend.supports_tools or not require_tools)
            ):
                return RoutingDecision(
                    backend=backend,
                    classification=classification,
                    reason=f"fallback to local backend {alias}",
                    rejected=tuple(rejected),
                )

        if classification.requires_sovereign_backend:
            raise SovereigntyError(
                "no sovereign backend available for classified content; refusing to "
                "route to external infrastructure",
                classification=str(classification),
                rejected=rejected,
            )
        raise ModelGatewayError(
            "no model backend can serve this request",
            classification=str(classification),
            purpose=purpose,
            rejected=rejected,
        )

    def assert_allowed(self, alias: str, classification: Classification) -> None:
        """Last-line check immediately before the call leaves the process.

        ``route`` already decided, but state can be mutated between deciding and calling
        (a tool result arriving with a higher classification, a retry reusing a stale
        alias). This is cheap and it is the check that actually protects the wire.
        """
        backend = self.get(alias)
        if not backend.accepts(classification):
            raise SovereigntyError(
                "refusing to send content to a backend that may not receive it",
                alias=alias,
                sovereignty=str(backend.sovereignty),
                classification=str(classification),
                backend_ceiling=str(backend.max_classification),
            )

    # ------------------------------------------------------------ construction

    @classmethod
    def from_litellm_config(cls, config: Mapping[str, object]) -> ModelPolicy:
        """Build the policy from a parsed ``litellm.yaml``.

        A backend that does not declare its sovereignty is treated as **external** and
        capped at C2. Forgetting the metadata must not silently create a channel for
        classified data.
        """
        raw_models = config.get("model_list")
        if not isinstance(raw_models, list):
            raise ModelGatewayError("litellm config has no model_list")

        backends: list[ModelBackend] = []
        for entry in raw_models:
            if not isinstance(entry, dict):
                continue
            alias = entry.get("model_name")
            if not isinstance(alias, str):
                continue
            info = entry.get("model_info")
            info_map: Mapping[str, object] = info if isinstance(info, dict) else {}
            meta = info_map.get("metadata")
            meta_map: Mapping[str, object] = meta if isinstance(meta, dict) else {}

            declared = str(meta_map.get("sovereignty", "external")).lower()
            sovereignty = (
                Sovereignty.LOCAL if declared == Sovereignty.LOCAL.value else Sovereignty.EXTERNAL
            )
            ceiling = _parse_ceiling(meta_map.get("max_classification"), sovereignty)

            backends.append(
                ModelBackend(
                    alias=alias,
                    sovereignty=sovereignty,
                    max_classification=ceiling,
                    supports_tools=bool(info_map.get("supports_function_calling", False)),
                    purpose=str(info_map.get("mode", "chat")),
                )
            )
        return cls(backends)


def _parse_ceiling(raw: object, sovereignty: Sovereignty) -> Classification:
    """Resolve a declared ceiling, clamped to what the sovereignty allows."""
    fallback = (
        MAX_EXTERNAL_CLASSIFICATION if sovereignty is Sovereignty.EXTERNAL else Classification.C4
    )
    declared = fallback
    if isinstance(raw, str | int):
        try:
            declared = Classification.parse(raw)
        except ValueError:
            declared = fallback
    # Never trust an external backend's own claim above the hard maximum.
    if sovereignty is Sovereignty.EXTERNAL:
        return min(declared, MAX_EXTERNAL_CLASSIFICATION)
    return declared
