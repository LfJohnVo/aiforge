"""The request and verdict shapes every policy decision travels in.

One shape for four questions -- may this requester see this document, use this tool, send
this content to an external model, act at this autonomy level -- because they are asked in
the same places and land in the same ledger. Four bespoke payloads would drift.

The verdict carries **reasons**, not just a boolean. A denial with no reason is
unauditable: it cannot be explained to the user, defended in a review, or debugged.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import Classification
from agent_forge.core.state import Identity

__all__ = [
    "PolicyKind",
    "PolicyRequest",
    "PolicyVerdict",
    "combine",
]


class PolicyKind(StrEnum):
    """What is being decided. Maps one-to-one onto a Rego package."""

    KNOWLEDGE = "knowledge"
    TOOLS = "tools"
    MODELS = "models"
    AUTONOMY = "autonomy"

    @property
    def package(self) -> str:
        return f"peak.{self.value}"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PolicyRequest(_Model):
    """The input document. Composed before evaluation, never during.

    Composed before, because a policy that fetches its own facts is a policy whose
    decision cannot be reproduced in a test -- which is why the Rego base forbids
    ``http.send``.
    """

    kind: PolicyKind
    tenant_id: str
    user_id: str = ""
    groups: tuple[str, ...] = ()
    authenticated: bool = False
    # Ceiling the requester carries; classification of the content at stake.
    ceiling: Classification = Classification.C0
    classification: Classification = Classification.C0
    # Tool name, source id or model name, depending on `kind`.
    resource: str = ""
    action_category: str = ""
    autonomy_declared: AutonomyLevel = AutonomyLevel.A0
    sovereignty: str = ""
    acl_groups: tuple[str, ...] = ()
    attributes: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def for_identity(cls, kind: PolicyKind, identity: Identity, **extra: Any) -> Self:
        return cls(
            kind=kind,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id or "",
            groups=tuple(identity.groups),
            authenticated=identity.authenticated,
            ceiling=identity.classification_ceiling,
            **extra,
        )

    def to_input(self) -> dict[str, Any]:
        """The OPA ``input`` document. Enum values go as strings, as Rego reads them."""
        return {
            "kind": str(self.kind),
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "groups": list(self.groups),
            "authenticated": self.authenticated,
            "ceiling": str(self.ceiling),
            "classification": str(self.classification),
            "resource": self.resource,
            "action_category": self.action_category,
            "autonomy_declared": str(self.autonomy_declared),
            "sovereignty": self.sovereignty,
            "acl_groups": list(self.acl_groups),
            "attributes": dict(self.attributes),
        }

    def cache_key(self) -> str:
        """Everything that can change the answer, and nothing that cannot."""
        return "|".join(
            (
                str(self.kind),
                self.tenant_id,
                self.user_id,
                ",".join(sorted(self.groups)),
                str(self.ceiling),
                str(self.classification),
                self.resource,
                self.action_category,
                str(self.autonomy_declared),
                self.sovereignty,
                ",".join(sorted(self.acl_groups)),
            )
        )

    @property
    def needs_fresh_decision(self) -> bool:
        """C3/C4 and A2+ may never run on a stale or absent remote decision.

        Not configurable. ``fail_mode: permissive_c0c1`` relaxes the rule for C0/C1 only;
        this property is what stops it reaching anything that matters.
        """
        highest = max(self.ceiling, self.classification)
        return highest.requires_sovereign_backend or self.autonomy_declared.requires_approval


class PolicyVerdict(_Model):
    """The answer. ``allow`` plus why, and what the caller must still do."""

    allow: bool
    reasons: tuple[str, ...] = ()
    obligations: tuple[str, ...] = ()
    # Present when the decision narrows one of them. `None` means "no opinion", which
    # combines differently from a permissive value.
    ceiling: Classification | None = None
    autonomy: AutonomyLevel | None = None
    # True when served from an expired cache or produced without reaching the remote PDP.
    stale: bool = False
    source: str = "local"

    @classmethod
    def deny(cls, *reasons: str, source: str = "local", stale: bool = False) -> Self:
        return cls(allow=False, reasons=reasons, source=source, stale=stale)

    @classmethod
    def permit(cls, *reasons: str, source: str = "local", **extra: Any) -> Self:
        return cls(allow=True, reasons=reasons, source=source, **extra)

    @classmethod
    def from_opa(cls, payload: Any, *, stale: bool = False) -> Self:
        """Read a Rego decision document, defaulting every uncertainty to denial."""
        body = payload if isinstance(payload, dict) else {}
        return cls(
            allow=bool(body.get("allow", False)),
            reasons=tuple(str(r) for r in body.get("reasons", []) or ()),
            obligations=tuple(str(o) for o in body.get("obligations", []) or ()),
            ceiling=_parse(Classification, body.get("ceiling")),
            autonomy=_parse(AutonomyLevel, body.get("autonomy")),
            stale=stale,
            source="opa",
        )


def combine(*verdicts: PolicyVerdict) -> PolicyVerdict:
    """Merge decisions the strict way: the most restrictive one wins.

    The local Rego base and the platform's remote overlay both get a say, and the rule
    between them has one direction. An overlay can forbid what the base allows; it can
    never permit what the base forbids, because then a remote misconfiguration would be
    enough to open a tenant up.
    """
    present = [v for v in verdicts if v is not None]
    if not present:
        return PolicyVerdict.deny("no policy produced a decision")

    ceilings = [v.ceiling for v in present if v.ceiling is not None]
    autonomies = [v.autonomy for v in present if v.autonomy is not None]
    return PolicyVerdict(
        allow=all(v.allow for v in present),
        reasons=tuple(dict.fromkeys(r for v in present for r in v.reasons)),
        obligations=tuple(dict.fromkeys(o for v in present for o in v.obligations)),
        # Ceiling narrows by minimum, autonomy requirement rises by maximum: both move
        # towards less freedom, which is the only safe direction to merge in.
        ceiling=min(ceilings) if ceilings else None,
        autonomy=max(autonomies) if autonomies else None,
        stale=any(v.stale for v in present),
        source="+".join(dict.fromkeys(v.source for v in present)),
    )


def _parse(enum: Any, value: Any) -> Any:
    if value is None:
        return None
    try:
        return enum.parse(value)
    except (TypeError, ValueError):
        return None
