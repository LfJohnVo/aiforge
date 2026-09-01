"""Autonomy levels A0-A4 and the rule that resolves the effective one.

The rule has one direction: the effective level of an action is the **maximum** of every
source that has an opinion (the tool's own declaration, the instance profile, the policy
decision point). No layer can lower another layer's requirement -- that is what stops a
connector from declaring itself A1 to slip past human approval.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import IntEnum
from typing import Self


class AutonomyLevel(IntEnum):
    """How much independence an action is granted."""

    A0 = 0  # Read only, no side effects
    A1 = 1  # Reversible, low impact
    A2 = 2  # Externally visible effect -> human approval
    A3 = 3  # Touches a system of record -> elevated-role approval
    A4 = 4  # Irreversible or high impact -> two distinct approvers

    @classmethod
    def parse(cls, value: str | int | Self | None) -> AutonomyLevel:
        if value is None:
            raise ValueError("autonomy level is required")
        if isinstance(value, AutonomyLevel):
            return value
        if isinstance(value, int):
            return cls(value)
        text = value.strip().upper()
        if text in cls.__members__:
            return cls[text]
        raise ValueError(f"unknown autonomy level {value!r}; expected one of A0..A4")

    @classmethod
    def _missing_(cls, value: object) -> AutonomyLevel | None:
        if isinstance(value, str):
            text = value.strip().upper()
            if text in cls.__members__:
                return cls[text]
        return None

    def __str__(self) -> str:
        return self.name

    @property
    def requires_approval(self) -> bool:
        """A2 and above pause the graph until a human decides."""
        return self >= AutonomyLevel.A2

    @property
    def required_approvals(self) -> int:
        """A4 needs two distinct approvers; A2/A3 need one; A0/A1 need none."""
        if self >= AutonomyLevel.A4:
            return 2
        return 1 if self.requires_approval else 0

    @property
    def requires_elevated_role(self) -> bool:
        """A3 and above may only be approved by the elevated approver group."""
        return self >= AutonomyLevel.A3


def resolve(*levels: AutonomyLevel | str | int | None) -> AutonomyLevel:
    """The effective level: the strictest opinion wins.

    ``None`` means "no opinion" and is ignored. With no opinions at all the answer is A0,
    the most restrictive, not the most convenient.
    """
    known = [AutonomyLevel.parse(level) for level in levels if level is not None]
    return max(known) if known else AutonomyLevel.A0


class AutonomyMap:
    """The profile's autonomy policy: a default plus per-action-category overrides.

    Action categories are free strings chosen by the domain (``enviar_correo``,
    ``modificar_erp``). The core never interprets them; it only looks them up.
    """

    __slots__ = ("_default", "_overrides")

    def __init__(
        self,
        default: AutonomyLevel | str | int = AutonomyLevel.A0,
        overrides: dict[str, AutonomyLevel | str | int] | None = None,
    ) -> None:
        self._default = AutonomyLevel.parse(default)
        self._overrides = {
            key: AutonomyLevel.parse(value) for key, value in (overrides or {}).items()
        }

    @property
    def default(self) -> AutonomyLevel:
        return self._default

    @property
    def overrides(self) -> dict[str, AutonomyLevel]:
        return dict(self._overrides)

    def for_action(self, category: str | None) -> AutonomyLevel:
        """Profile's opinion for an action category, falling back to the default."""
        if category is None:
            return self._default
        return self._overrides.get(category, self._default)

    def effective(
        self,
        category: str | None,
        *,
        declared: AutonomyLevel | str | int | None = None,
        policy: AutonomyLevel | str | int | None = None,
    ) -> AutonomyLevel:
        """Combine the profile, the tool's own declaration and the PDP verdict.

        ``declared`` is ``ToolSpec.autonomy_min``; ``policy`` is what governance imposes.
        """
        return resolve(self.for_action(category), declared, policy)

    def categories(self) -> Iterable[str]:
        return self._overrides.keys()

    def __repr__(self) -> str:
        return f"AutonomyMap(default={self._default}, overrides={self._overrides})"
