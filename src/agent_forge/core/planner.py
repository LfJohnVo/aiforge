"""Planner: decomposes a request into steps.

This is the supervisor node of the graph and, importantly, the **re-entry point for
``replan``**: when the judge rejects an answer, the graph resumes here rather than
re-running the whole task.

Design bias: fewer steps. A plan step that does not change the final answer costs a
model call, a checkpoint write and latency, and buys nothing. Most questions are one
step; the planner is allowed to say so.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from agent_forge.core.classification import Classification
from agent_forge.core.errors import ModelGatewayError
from agent_forge.core.prompts import PromptRegistry
from agent_forge.core.state import PlanStep
from agent_forge.core.subgraphs.base import ToolDescriptor
from agent_forge.gateway.litellm_client import GovernedGateway
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

MAX_STEPS = 6


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """Everything the planner needs, with no reference to graph internals."""

    request: str
    area: str
    intents: Sequence[str] = ()
    tools: Sequence[ToolDescriptor] = ()
    classification: Classification = Classification.C0
    tenant_id: str = ""
    trace_id: str = ""
    max_steps: int = MAX_STEPS
    # On a replan the judge tells us what was wrong; ignoring it produces the same
    # answer twice and burns the retry budget.
    feedback: str = ""


class Planner:
    """Produces a plan, falling back to a single step when planning adds nothing."""

    def __init__(
        self,
        prompts: PromptRegistry,
        *,
        gateway: GovernedGateway | None = None,
        model_alias: str = "local/fast",
    ) -> None:
        self._prompts = prompts
        self._gateway = gateway
        self._model_alias = model_alias

    async def plan(self, request: PlanRequest) -> list[PlanStep]:
        """Return the ordered steps. Never empty."""
        if self._gateway is None:
            return [_single_step(request)]

        prompt = self._prompts.render(
            "planner",
            request=_with_feedback(request),
            area=request.area,
            intents=list(request.intents),
            tools=[
                {
                    "name": tool.name,
                    "description": tool.description,
                    "autonomy_min": str(tool.autonomy_min),
                }
                for tool in request.tools
            ],
            max_steps=request.max_steps,
        )

        try:
            response = await self._gateway.complete(
                [{"role": "user", "content": prompt}],
                classification=request.classification,
                preferred=[self._model_alias],
                tenant_id=request.tenant_id,
                trace_id=request.trace_id,
                temperature=0.0,
                max_tokens=800,
            )
        except ModelGatewayError as exc:
            # A planner outage must degrade to a direct answer, not to a failed request.
            log.warning("planner.unavailable", detail=str(exc))
            return [_single_step(request)]

        steps = parse_plan(response.content, request)
        if not steps:
            log.debug("planner.unparseable_output_using_single_step")
            return [_single_step(request)]

        known = {tool.name for tool in request.tools}
        cleaned = _drop_unknown_tools(steps, known)
        return cleaned[: request.max_steps]


def parse_plan(raw: str, request: PlanRequest) -> list[PlanStep]:
    """Parse the planner's JSON, tolerating the fences models like to add."""
    payload = _extract_json(raw)
    if payload is None:
        return []
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        return []

    steps: list[PlanStep] = []
    for entry in raw_steps[: request.max_steps]:
        if not isinstance(entry, dict):
            continue
        description = str(entry.get("description") or "").strip()
        if not description:
            continue
        tool = entry.get("tool")
        category = entry.get("action_category")
        steps.append(
            PlanStep(
                description=description,
                tool=str(tool) if isinstance(tool, str) and tool else None,
                action_category=str(category) if isinstance(category, str) and category else None,
                args=entry.get("args") if isinstance(entry.get("args"), dict) else {},
            )
        )
    return steps


def _drop_unknown_tools(steps: list[PlanStep], known: set[str]) -> list[PlanStep]:
    """Strip hallucinated tool names, keeping the step as pure reasoning.

    Dropping the whole step would lose the intent; keeping a nonexistent tool would fail
    at execution. Neutering the tool reference is the behaviour that degrades gracefully.
    """
    cleaned: list[PlanStep] = []
    for step in steps:
        if step.tool is not None and step.tool not in known:
            log.debug("planner.dropped_unknown_tool", tool=step.tool)
            cleaned.append(step.model_copy(update={"tool": None, "args": {}}))
        else:
            cleaned.append(step)
    return cleaned


def _single_step(request: PlanRequest) -> PlanStep:
    return PlanStep(description=request.request or "Responder la peticion del usuario.")


def _with_feedback(request: PlanRequest) -> str:
    if not request.feedback:
        return request.request
    return f"{request.request}\n\nIntento anterior rechazado. Corrige esto: {request.feedback}"


def _extract_json(raw: str) -> dict[str, object] | None:
    """Find the first JSON object in the text, fences and prose included."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
