"""The connector contract (Appendix B of the master prompt).

A connector turns an external system into typed tools. Three obligations, and they are
obligations rather than conventions because the registry cannot check honesty:

1. **``autonomy_min`` reflects what the tool actually does.** The effective level is the
   maximum of this, the profile and the PDP, so declaring low is the only way to slip
   past human approval.
2. **``ToolResult.classification`` reflects what the tool returned.** A tool result can
   raise the task's cumulative classification and with it force a sovereign backend.
   Marking a result C0 when it is not is a data leak, not a rounding error.
3. **``health()`` actually probes.** A connector that is not healthy is not registered,
   so its tools do not exist for the model. A false positive means the model sees tools
   that will fail.

Everything else here -- timeouts, the circuit breaker, argument digests -- exists so a
connector author does not have to reimplement them and get them subtly wrong.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import Classification
from agent_forge.core.errors import AgentForgeError, ToolError
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

ENTRY_POINT_GROUP = "agent_forge.connectors"
DEFAULT_TIMEOUT = 30.0


class ToolSpec(BaseModel):
    """What a tool is and what it takes to run it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    required_scope: str = ""
    # Deliberately A2 by default. A connector author who forgets to think about autonomy
    # gets human approval, not silent execution.
    autonomy_min: AutonomyLevel = AutonomyLevel.A2
    # Highest classification this tool's results may carry. Used to reject a call whose
    # results could not be shown to the requester anyway.
    max_classification: Classification = Classification.C4
    readonly: bool = False

    @property
    def connector(self) -> str:
        return self.name.split(".", 1)[0] if "." in self.name else self.name

    def to_openai_tool(self) -> dict[str, Any]:
        """The shape a model expects when tools are offered to it."""
        return {
            "type": "function",
            "function": {
                "name": self.name.replace(".", "__"),
                "description": self.description,
                "parameters": self.input_schema
                or {"type": "object", "properties": {}, "additionalProperties": False},
            },
        }


class CallContext(BaseModel):
    """Who is calling, and what they are entitled to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1)
    user_id: str = ""
    groups: tuple[str, ...] = ()
    classification_ceiling: Classification = Classification.C0
    trace_id: str = ""
    task_id: str = ""
    autonomy_granted: AutonomyLevel = AutonomyLevel.A0

    @property
    def is_anonymous(self) -> bool:
        return not self.user_id


class ToolResult(BaseModel):
    """What a tool returned, and how sensitive it is."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    # Default C4, not C0: a connector that forgets to classify its output must not have
    # that output treated as public. Over-classifying costs a local model call.
    classification: Classification = Classification.C4
    evidence_digest: str | None = None
    duration_ms: int = 0

    @classmethod
    def success(
        cls,
        data: dict[str, Any],
        *,
        classification: Classification = Classification.C4,
        duration_ms: int = 0,
    ) -> ToolResult:
        return cls(
            ok=True,
            data=data,
            classification=classification,
            evidence_digest=digest(data),
            duration_ms=duration_ms,
        )

    @classmethod
    def failure(cls, error: str, *, duration_ms: int = 0) -> ToolResult:
        return cls(ok=False, error=error, classification=Classification.C0, duration_ms=duration_ms)


@runtime_checkable
class BaseConnector(Protocol):
    """Every connector satisfies this. Registered via entry points."""

    name: str
    version: str

    async def health(self) -> bool: ...

    def capabilities(self) -> Sequence[ToolSpec]: ...

    async def invoke(self, tool: str, args: dict[str, Any], ctx: CallContext) -> ToolResult: ...

    async def aclose(self) -> None: ...


# --------------------------------------------------------------------- helpers


def digest(payload: Any) -> str:
    """Stable digest. The ledger records this, never the values themselves."""
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CircuitBreaker:
    """Stops hammering a system that is already down.

    A connector whose backend is failing should fail fast rather than tie up the graph on
    a timeout per call. After ``threshold`` consecutive failures the breaker opens for
    ``cooldown`` seconds; the first call afterwards is allowed through to test the water.
    """

    __slots__ = ("_cooldown", "_failures", "_opened_at", "_threshold")

    def __init__(self, *, threshold: int = 5, cooldown: float = 30.0) -> None:
        self._threshold = max(1, threshold)
        self._cooldown = cooldown
        self._failures = 0
        self._opened_at = 0.0

    @property
    def is_open(self) -> bool:
        if self._failures < self._threshold:
            return False
        if time.monotonic() - self._opened_at >= self._cooldown:
            # Half-open: let one call through rather than staying shut forever.
            self._failures = self._threshold - 1
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = time.monotonic()

    def state(self) -> str:
        return "open" if self.is_open else "closed"


async def guard[T](
    operation: Callable[[], Awaitable[T]],
    *,
    breaker: CircuitBreaker,
    # The deadline belongs to the connector's own configuration, not to an ambient
    # cancel scope the caller owns, so it is a parameter rather than inherited.
    timeout: float = DEFAULT_TIMEOUT,  # noqa: ASYNC109
    what: str = "tool call",
) -> T:
    """Run an external call with a timeout and a circuit breaker.

    Every connector's I/O goes through this, which is how "timeouts and circuit breakers
    on all external I/O" stays true as connectors are added.
    """
    if breaker.is_open:
        raise ToolError("circuit breaker is open; the backend is failing", what=what)
    try:
        async with asyncio.timeout(timeout):
            result = await operation()
    except TimeoutError as exc:
        breaker.record_failure()
        raise ToolError("timed out", what=what, timeout_seconds=timeout) from exc
    except AgentForgeError:
        breaker.record_failure()
        raise
    except Exception as exc:
        breaker.record_failure()
        raise ToolError("failed", what=what, detail=type(exc).__name__) from exc
    breaker.record_success()
    return result


@dataclass(slots=True)
class ConnectorBase:
    """Optional base class with the bookkeeping every connector needs.

    Implementing the Protocol directly is fine; this just saves repeating the breaker,
    the timeout and the timing.
    """

    name: str
    version: str = "1.0.0"
    timeout: float = DEFAULT_TIMEOUT
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)

    def capabilities(self) -> Sequence[ToolSpec]:  # pragma: no cover - overridden
        return ()

    async def health(self) -> bool:  # pragma: no cover - overridden
        return True

    async def aclose(self) -> None:
        return None

    def spec_for(self, tool: str) -> ToolSpec | None:
        return next((s for s in self.capabilities() if s.name == tool), None)

    async def run(
        self,
        what: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
        *,
        classification: Classification = Classification.C4,
    ) -> ToolResult:
        """Execute one call with guards, timing and uniform error shaping."""
        started = time.monotonic()
        try:
            data = await guard(operation, breaker=self.breaker, timeout=self.timeout, what=what)
        except AgentForgeError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            log.warning("connector.failed", connector=self.name, tool=what, code=exc.code)
            return ToolResult.failure(exc.message, duration_ms=elapsed)
        elapsed = int((time.monotonic() - started) * 1000)
        return ToolResult.success(data, classification=classification, duration_ms=elapsed)
