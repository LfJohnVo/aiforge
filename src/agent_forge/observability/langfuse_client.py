"""Langfuse: LLM traces, costs and judge scores in one place.

Optional (extra ``evals``) and disabled unless keys are configured. When it is off every
method here is a no-op, so instrumentation call sites never have to guard.

The same rule as OTel applies, harder: Langfuse is a **hosted** product by default, and
its whole value is that it shows prompts and completions. So what reaches it is decided
here, not at the call site:

* ``C0``/``C1`` -- prompt and completion are sent. That is the point of the tool.
* ``C2`` and above -- only digests, token counts and scores. A confidential answer does
  not leave the perimeter to make a dashboard prettier.

A self-hosted Langfuse inside the perimeter can lift that with ``max_content_class``, and
that decision belongs to whoever runs the deployment, not to this module.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from agent_forge.core.classification import Classification
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = ["LangfuseTracer", "build_langfuse"]


def _digest(text: str) -> str:
    """SHA-256 of the text.

    Deliberately the same computation ``events.evidence.digest`` performs on a string, so
    a redacted Langfuse entry and a ledger record can be matched by eye -- without this
    module importing the events package, which would make observability depend on the
    layer above it.
    """
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(slots=True)
class LangfuseTracer:
    """Wraps the Langfuse client, or nothing at all."""

    client: Any | None = None
    # Highest classification whose text may be sent. C1 by default: a hosted Langfuse is
    # outside the perimeter, and C2 is "confidential".
    max_content_class: Classification = Classification.C1

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def trace_task(self, state: Any, *, scores: dict[str, float] | None = None) -> None:
        """Record one finished task."""
        if self.client is None:
            return
        classification = _classification_of(state)
        redact = classification > self.max_content_class
        identity = getattr(state, "identity", None)
        tenant = getattr(identity, "tenant_id", "") if identity else ""

        try:
            observation = self.client.start_observation(
                name=f"task.{getattr(state, 'area', 'general')}",
                as_type="generation",
                input=self._input(state, redact=redact),
                output=self._output(state, redact=redact),
                metadata={
                    "tenant_id": tenant,
                    "task_id": getattr(state, "task_id", ""),
                    "thread_id": getattr(state, "thread_id", ""),
                    "classification": str(classification),
                    "autonomy": str(getattr(state, "autonomy", "")),
                    "citations": [c.reference for c in getattr(state, "citations", [])],
                    "retries": getattr(state, "retries", 0),
                    "redacted": redact,
                },
                usage_details=self._usage(state),
                cost_details={"total": float(getattr(state.usage, "cost_usd", 0.0) or 0.0)},
            )
            observation.end()
            for name, value in (scores or getattr(state, "judge_scores", {}) or {}).items():
                self.client.create_score(name=name, value=float(value), data_type="NUMERIC")
        except Exception as exc:  # observability must never break the request
            log.warning("langfuse.trace_failed", detail=f"{type(exc).__name__}: {exc}"[:200])

    def score(self, name: str, value: float, *, comment: str = "") -> None:
        """Attach a score to the current trace. Used by the eval harness."""
        if self.client is None:
            return
        try:
            self.client.create_score(
                name=name, value=float(value), data_type="NUMERIC", comment=comment[:500]
            )
        except Exception as exc:
            log.warning("langfuse.score_failed", detail=f"{type(exc).__name__}: {exc}"[:200])

    def flush(self) -> None:
        if self.client is not None:
            try:
                self.client.flush()
            except Exception as exc:  # pragma: no cover - shutdown path
                log.warning("langfuse.flush_failed", detail=str(exc)[:200])

    def shutdown(self) -> None:
        if self.client is not None:
            try:
                self.client.shutdown()
            except Exception as exc:  # pragma: no cover - shutdown path
                log.warning("langfuse.shutdown_failed", detail=str(exc)[:200])

    # ------------------------------------------------------------------ payload

    def _input(self, state: Any, *, redact: bool) -> Any:
        question = getattr(state, "last_user_message", "")
        if redact:
            return {"digest": _digest(question), "chars": len(question)}
        return question

    def _output(self, state: Any, *, redact: bool) -> Any:
        answer = getattr(state, "answer", "")
        if redact:
            return {"digest": _digest(answer), "chars": len(answer)}
        return answer

    @staticmethod
    def _usage(state: Any) -> dict[str, int]:
        usage = getattr(state, "usage", None)
        return {
            "input": int(getattr(usage, "tokens_in", 0) or 0),
            "output": int(getattr(usage, "tokens_out", 0) or 0),
        }


def _classification_of(state: Any) -> Classification:
    try:
        return Classification.parse(getattr(state, "classification", Classification.C0))
    except (TypeError, ValueError):
        # Unparseable means treat it as the most sensitive, which here means send nothing.
        return Classification.C4


def build_langfuse(
    *,
    public_key: str = "",
    secret_key: str = "",
    host: str = "",
    environment: str = "development",
    enabled: bool = True,
    max_content_class: Classification = Classification.C1,
) -> LangfuseTracer:
    """Construct the tracer, or a disabled one.

    Missing keys disable it rather than failing: a cell must start on a laptop with no
    Langfuse, and the profile enabling it is not the same thing as the operator having
    provisioned it.
    """
    if not enabled or not (public_key and secret_key):
        log.info("langfuse.disabled", configured=bool(public_key and secret_key))
        return LangfuseTracer()
    try:
        from langfuse import Langfuse
    except ImportError:
        log.warning(
            "langfuse.not_installed",
            detail="profile enables Langfuse but the `evals` extra is not installed",
        )
        return LangfuseTracer()

    client = Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        host=host or None,
        environment=environment,
        # The cell owns its own tracer provider; Langfuse must not replace it or the OTel
        # spans and the Langfuse traces stop sharing a trace id.
        tracing_enabled=True,
    )
    log.info("langfuse.enabled", host=host or "cloud", max_content_class=str(max_content_class))
    return LangfuseTracer(client=client, max_content_class=max_content_class)
