"""Model gateway: routing by data classification, then transport.

`model_policy` is the only module allowed to choose a backend; `GovernedGateway` is the
only way to reach one.
"""

from __future__ import annotations

from agent_forge.gateway.litellm_client import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    GovernedGateway,
    LiteLLMTransport,
    ModelTransport,
)
from agent_forge.gateway.model_policy import (
    ModelBackend,
    ModelPolicy,
    RoutingDecision,
    Sovereignty,
)

__all__ = [
    "ChatChunk",
    "ChatRequest",
    "ChatResponse",
    "GovernedGateway",
    "LiteLLMTransport",
    "ModelBackend",
    "ModelPolicy",
    "ModelTransport",
    "RoutingDecision",
    "Sovereignty",
]
