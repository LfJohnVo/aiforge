"""n8n connector: fire a workflow, then wait for it to come back.

An n8n workflow is not a function call. It takes seconds or hours, it may involve a human,
and it reports back out of band. So this connector has two halves:

* **Trigger** -- POST to the workflow's webhook with a ``correlation_id``, then wait for
  the callback up to a bounded time. Nothing is invented if it does not arrive: the tool
  returns "accepted, still running" with the correlation id, which the graph reports
  honestly rather than pretending the work finished.
* **Callback** -- ``POST /channels/n8n/callback`` resolves the waiter. The API layer wires
  that route; this module owns the correlation table so a callback that arrives after the
  waiter timed out is still recorded rather than dropped.

The reverse direction (n8n calling the cell) needs nothing here: n8n uses the ordinary
OpenAI-compatible channel like any other client.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from agent_forge.connectors.base import (
    CallContext,
    CircuitBreaker,
    ConnectorBase,
    ToolResult,
    ToolSpec,
)
from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import Classification
from agent_forge.core.errors import ToolError
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = ["CallbackRegistry", "N8nConnector", "WorkflowSpec"]

DEFAULT_WAIT_SECONDS = 60.0
# How long a callback that nobody is waiting for is kept, so a late arrival can still be
# correlated by an operator looking at the task.
ORPHAN_TTL_SECONDS = 900.0


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    """One workflow the profile exposes as a tool."""

    name: str
    description: str = ""
    webhook_path: str = ""
    # Workflows do things in the world. A2 unless the profile deliberately lowers it.
    autonomy_min: AutonomyLevel = AutonomyLevel.A2
    wait_seconds: float = DEFAULT_WAIT_SECONDS
    input_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> str:
        return self.webhook_path or f"webhook/{self.name}"


@dataclass(slots=True)
class _Pending:
    future: asyncio.Future[dict[str, Any]]
    created_at: float


class CallbackRegistry:
    """Correlation table between a triggered workflow and its callback.

    Lives outside the connector so the HTTP route and the tool call can reach the same
    table, and so a test can drive both halves without a server.
    """

    def __init__(self, *, orphan_ttl: float = ORPHAN_TTL_SECONDS) -> None:
        self._pending: dict[str, _Pending] = {}
        self._orphans: dict[str, tuple[dict[str, Any], float]] = {}
        self._orphan_ttl = orphan_ttl

    def register(self, correlation_id: str) -> asyncio.Future[dict[str, Any]]:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[correlation_id] = _Pending(future=future, created_at=time.monotonic())
        return future

    def resolve(self, correlation_id: str, payload: dict[str, Any]) -> bool:
        """Deliver a callback. Returns False when nobody was waiting.

        A late callback is kept rather than discarded: the waiter timing out does not mean
        the workflow did not run, and an operator inspecting the task should be able to
        see what came back.
        """
        pending = self._pending.pop(correlation_id, None)
        if pending is not None and not pending.future.done():
            pending.future.set_result(payload)
            return True
        self._prune()
        self._orphans[correlation_id] = (payload, time.monotonic())
        log.info("n8n.callback_orphaned", correlation_id=correlation_id)
        return False

    def take_orphan(self, correlation_id: str) -> dict[str, Any] | None:
        self._prune()
        entry = self._orphans.pop(correlation_id, None)
        return entry[0] if entry else None

    def cancel(self, correlation_id: str) -> None:
        pending = self._pending.pop(correlation_id, None)
        if pending is not None and not pending.future.done():
            pending.future.cancel()

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._orphan_ttl
        for key, (_, arrived) in list(self._orphans.items()):
            if arrived < cutoff:
                del self._orphans[key]

    def stats(self) -> dict[str, int]:
        return {"pending": len(self._pending), "orphans": len(self._orphans)}


class N8nConnector(ConnectorBase):
    """Triggers n8n workflows and correlates their callbacks."""

    def __init__(
        self,
        base_url: str = "",
        *,
        api_key: str = "",
        workflows: Sequence[WorkflowSpec] = (),
        callback_url: str = "",
        registry: CallbackRegistry | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(name="n8n", version="1.0.0", timeout=timeout, breaker=CircuitBreaker())
        self._base_url = base_url.rstrip("/")
        self._workflows = {w.name: w for w in workflows}
        self._callback_url = callback_url.rstrip("/")
        self.callbacks = registry or CallbackRegistry()
        self._client = client or (
            httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(timeout, connect=10.0),
                headers={"x-n8n-api-key": api_key} if api_key else {},
            )
            if base_url
            else None
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> N8nConnector:
        source = env if env is not None else dict(os.environ)
        base = source.get("N8N_URL", "")
        public = source.get("AGENT_PUBLIC_URL", "")
        return cls(
            base,
            api_key=source.get("N8N_API_KEY", ""),
            callback_url=f"{public}/channels/n8n/callback" if public else "",
        )

    def capabilities(self) -> Sequence[ToolSpec]:
        return tuple(
            ToolSpec(
                name=f"n8n.{workflow.name}",
                description=workflow.description or f"Ejecuta el workflow {workflow.name}",
                input_schema=workflow.input_schema
                or {"type": "object", "properties": {}, "additionalProperties": True},
                required_scope=f"n8n:{workflow.name}",
                autonomy_min=workflow.autonomy_min,
                max_classification=Classification.C4,
                readonly=False,
            )
            for workflow in self._workflows.values()
        )

    async def health(self) -> bool:
        if self._client is None:
            return False
        try:
            response = await self._client.get("/healthz", timeout=5.0)
        except httpx.HTTPError:
            return False
        return response.status_code < 500

    async def invoke(self, tool: str, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        workflow = self._workflows.get(tool.removeprefix("n8n."))
        if workflow is None:
            return ToolResult.failure(f"unknown workflow {tool}")
        if self._client is None:
            return ToolResult.failure("n8n is not configured for this cell")

        correlation_id = str(uuid.uuid4())

        async def call() -> dict[str, Any]:
            return await self._trigger_and_wait(workflow, args, ctx, correlation_id)

        return await self.run(tool, call, classification=Classification.C4)

    async def _trigger_and_wait(
        self,
        workflow: WorkflowSpec,
        args: dict[str, Any],
        ctx: CallContext,
        correlation_id: str,
    ) -> dict[str, Any]:
        assert self._client is not None
        waiter = self.callbacks.register(correlation_id)
        payload = {
            "correlation_id": correlation_id,
            "callback_url": self._callback_url,
            "tenant_id": ctx.tenant_id,
            "trace_id": ctx.trace_id,
            "task_id": ctx.task_id,
            # The end user's identity travels so the workflow can enforce its own rules.
            "requested_by": ctx.user_id,
            "input": args,
        }

        try:
            response = await self._client.post(f"/{workflow.path}", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            self.callbacks.cancel(correlation_id)
            raise ToolError(
                "could not trigger the n8n workflow",
                workflow=workflow.name,
                detail=str(exc),
            ) from exc

        try:
            result = await asyncio.wait_for(waiter, timeout=workflow.wait_seconds)
        except TimeoutError:
            self.callbacks.cancel(correlation_id)
            log.info(
                "n8n.callback_timeout",
                workflow=workflow.name,
                correlation_id=correlation_id,
            )
            # Accepted but unfinished. Reported as such, never as a completed action:
            # claiming the workflow finished is exactly the lie the graph must not tell.
            return {
                "status": "accepted",
                "workflow": workflow.name,
                "correlation_id": correlation_id,
                "detail": (
                    "el workflow se lanzo y sigue en curso; el resultado llegara por callback"
                ),
            }

        log.info("n8n.completed", workflow=workflow.name, correlation_id=correlation_id)
        return {
            "status": "completed",
            "workflow": workflow.name,
            "correlation_id": correlation_id,
            "output": result,
        }

    def deliver_callback(self, correlation_id: str, payload: dict[str, Any]) -> bool:
        """Called by the HTTP route when n8n reports back."""
        return self.callbacks.resolve(correlation_id, payload)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
