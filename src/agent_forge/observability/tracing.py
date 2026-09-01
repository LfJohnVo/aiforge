"""OpenTelemetry tracing, with the attribute discipline enforced in code.

Instrumenting a system that handles classified data has one rule that overrides
convenience: **a span never carries content.** Not the prompt, not the answer, not the
tool arguments, not the retrieved chunk. Traces leave the perimeter -- that is what a
collector is for -- and a span attribute is the easiest place in a codebase to leak a
document into a system nobody classified.

So ``span()`` takes a fixed set of correlation attributes and refuses the rest. The
collector strips content attributes too (``deploy/observability/otel-collector.yaml``),
but defence in depth means the application must not send them in the first place.

Tracing is optional. Without an OTLP endpoint the module installs no exporter and
``span()`` becomes a no-op context manager, so a laptop runs the same code path as a
cluster and nothing has to be guarded with ``if tracing_enabled``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "CORRELATION_KEYS",
    "current_trace_id",
    "setup_tracing",
    "shutdown_tracing",
    "span",
    "span_for_state",
]

# The only attributes a span may carry, beyond the numeric measurements a node adds.
# Everything here is an identifier or a level -- nothing that could be text a user wrote
# or a document contained.
CORRELATION_KEYS = frozenset(
    {
        "tenant_id",
        "agent_id",
        "task_id",
        "thread_id",
        "trace_id",
        "area",
        "autonomy_level",
        "classification",
        "intent",
        "model",
        "backend",
        "sovereignty",
        "connector",
        "tool",
        "node",
        "decision",
        "effect",
        "verdict",
        "layer",
        "source_count",
        "result_count",
        "token_count",
        "retries",
        "cache_hit",
        "ok",
        "error.type",
    }
)

# Anything matching these is content or a credential. Sending one is a defect, so it is
# dropped and reported rather than silently passed on.
_FORBIDDEN_SUBSTRINGS = ("prompt", "message", "content", "answer", "text", "secret", "key")

_TRACER: Any = None
_PROVIDER: Any = None


def setup_tracing(
    *,
    endpoint: str = "",
    service: str = "agent-forge",
    instance: str = "default",
    environment: str = "development",
) -> Any:
    """Install the tracer provider. Idempotent; a missing endpoint disables tracing.

    Disabled rather than buffered: an exporter with nowhere to send accumulates spans in
    memory and eventually drops them anyway, having spent the memory.
    """
    global _TRACER, _PROVIDER
    if _TRACER is not None:
        return _TRACER
    if not endpoint:
        log.info("tracing.disabled", detail="no OTLP endpoint configured")
        return None

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:  # pragma: no cover - the SDK is a core dependency
        log.warning("tracing.sdk_missing")
        return None

    resource = Resource.create(
        {
            "service.name": service,
            "service.instance.id": instance,
            "deployment.environment": environment,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _PROVIDER = provider
    _TRACER = trace.get_tracer("agent_forge")
    log.info("tracing.enabled", endpoint=endpoint, service=service, instance=instance)
    return _TRACER


def shutdown_tracing() -> None:
    """Flush pending spans. Called on shutdown so the last trace of a run is not lost."""
    global _TRACER, _PROVIDER
    if _PROVIDER is not None:
        _PROVIDER.shutdown()
    _TRACER = None
    _PROVIDER = None


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Open a span. A no-op when tracing is off.

    Attribute names outside ``CORRELATION_KEYS`` are dropped and logged: the guard is
    here rather than at each call site because there are dozens of call sites and one
    careless ``answer=...`` is all it takes.
    """
    safe = _safe_attributes(name, attributes)
    if _TRACER is None:
        yield None
        return
    # `record_exception` and `set_status_on_exception` are both True by default, and
    # both write the exception *message* onto the span. A message routinely quotes the
    # input that caused it -- a rejected document, a chunk of retrieved text -- so the
    # SDK's own handling is turned off and `_set_error` records the type alone.
    with _TRACER.start_as_current_span(
        name,
        attributes=safe,
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except Exception as exc:
            _set_error(current, exc)
            raise


def span_for_state(name: str, state: Any, **extra: Any) -> Any:
    """A span pre-filled with the correlation attributes every span must carry."""
    identity = getattr(state, "identity", None)
    return span(
        name,
        tenant_id=getattr(identity, "tenant_id", "") if identity else "",
        agent_id=getattr(state, "agent_name", ""),
        area=getattr(state, "area", ""),
        task_id=getattr(state, "task_id", ""),
        thread_id=getattr(state, "thread_id", ""),
        autonomy_level=str(getattr(state, "autonomy", "")),
        classification=str(getattr(state, "classification", "")),
        **extra,
    )


def current_trace_id() -> str:
    """The active W3C trace id, or empty when tracing is off."""
    if _TRACER is None:
        return ""
    from opentelemetry import trace

    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return ""
    return format(context.trace_id, "032x")


def _safe_attributes(name: str, attributes: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in attributes.items():
        if value is None or value == "":
            continue
        if key not in CORRELATION_KEYS or _looks_like_content(key):
            log.warning(
                "tracing.attribute_rejected",
                span=name,
                attribute=key,
                detail="only correlation attributes may be traced; never content",
            )
            continue
        safe[key] = value if isinstance(value, int | float | bool) else str(value)
    return safe


def _looks_like_content(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _FORBIDDEN_SUBSTRINGS)


def _set_error(current: Any, exc: BaseException) -> None:
    """Mark the span failed, recording the exception **type** and nothing else.

    Deliberately not ``record_exception``: that writes the message and the stack trace
    onto the span, and an exception message routinely quotes the input that caused it --
    a rejected document, a malformed field, a chunk of retrieved text. The type is what an
    operator needs to see in a trace; the message belongs in the log, where the redaction
    processor can reach it.
    """
    from opentelemetry.trace import Status, StatusCode

    current.set_status(Status(StatusCode.ERROR, type(exc).__name__))
    current.set_attribute("error.type", type(exc).__name__)
