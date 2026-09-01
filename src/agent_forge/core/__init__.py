"""Domain-neutral kernel: graph, state, autonomy, classification, errors.

Nothing in this package may know about a business domain. That invariant is checked
mechanically by the repository graph skill.
"""

from __future__ import annotations

from agent_forge.core.autonomy import AutonomyLevel, AutonomyMap
from agent_forge.core.classification import Classification
from agent_forge.core.errors import AgentForgeError

__all__ = ["AgentForgeError", "AutonomyLevel", "AutonomyMap", "Classification"]
