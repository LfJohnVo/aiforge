"""Data classification C0-C4.

The classification of a task is **cumulative**: the maximum of the request, of every
retrieved chunk and of every tool result. Whoever decides which model backend to use
must read that maximum, never the request alone -- see ``gateway/model_policy.py``,
which is the only place in the codebase allowed to make that decision.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Self


class Classification(IntEnum):
    """Sensitivity of a piece of content.

    An ``IntEnum`` on purpose: "is this above the ceiling" is the question asked on every
    retrieved chunk and every tool result, and it must be a comparison, not a lookup
    table someone can get wrong.
    """

    C0 = 0  # Public
    C1 = 1  # Internal
    C2 = 2  # Confidential
    C3 = 3  # Restricted
    C4 = 4  # Secret

    @classmethod
    def parse(cls, value: str | int | Self | None) -> Classification:
        """Accept "C2", "c2", 2 or the enum itself. Reject anything else loudly."""
        if value is None:
            raise ValueError("classification is required; there is no implicit default")
        if isinstance(value, Classification):
            return value
        if isinstance(value, int):
            return cls(value)
        text = value.strip().upper()
        if text in cls.__members__:
            return cls[text]
        raise ValueError(f"unknown classification {value!r}; expected one of C0..C4")

    @classmethod
    def _missing_(cls, value: object) -> Classification | None:
        if isinstance(value, str):
            text = value.strip().upper()
            if text in cls.__members__:
                return cls[text]
        return None

    def __str__(self) -> str:
        return self.name

    @property
    def requires_sovereign_backend(self) -> bool:
        """C3 and C4 may never leave locally controlled infrastructure."""
        return self >= Classification.C3


# Highest level an external, contractually governed model may ever see. This is a
# constant rather than configuration on purpose: it is the boundary the whole product
# is built to guarantee, and a config knob would make it negotiable.
MAX_EXTERNAL_CLASSIFICATION: Classification = Classification.C2


def accumulate(*values: Classification | str | int | None) -> Classification:
    """Combine classifications, ignoring ``None``. Empty input is C0.

    Used to fold chunk and tool-result classifications into the task's running maximum.
    """
    known = [Classification.parse(v) for v in values if v is not None]
    return max(known) if known else Classification.C0


def is_within(value: Classification, ceiling: Classification) -> bool:
    """True when ``value`` may be shown to a requester whose ceiling is ``ceiling``."""
    return value <= ceiling


def coerce_payload(raw: Any) -> Classification:
    """Read a classification out of untrusted payload, defaulting to the safest answer.

    Ingested documents and remote tool results carry their own label. A malformed or
    absent label must not become C0: unclassifiable content is treated as C4 so that it
    can never route to an external backend by accident. The ingestion pipeline applies
    the profile default *before* content reaches here; anything still unlabelled at this
    point is genuinely unknown.
    """
    try:
        return Classification.parse(raw)
    except (ValueError, TypeError):
        return Classification.C4
