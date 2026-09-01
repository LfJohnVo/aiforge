"""Structured logging.

Logs go to stdout as JSON (12-factor). Every record carries the correlation fields an
operator needs -- ``trace_id``, ``tenant_id``, ``task_id`` -- and never carries message
content, PII or secrets: the ledger records digests, the logs record facts.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, unbind_contextvars
from structlog.typing import EventDict, WrappedLogger

# Keys that must never appear in a log line. Logging one of these is a defect, so the
# processor replaces the value rather than dropping the record: the redaction is
# visible in the output and shows up in review.
_FORBIDDEN_KEYS = frozenset(
    {
        "prompt",
        "messages",
        "content",
        "answer",
        "password",
        "api_key",
        "token",
        "secret",
        "authorization",
        "dsn",
    }
)
_REDACTED = "[redacted]"


def redact_sensitive(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Replace values of known-sensitive keys, recursively."""
    return _redact_mapping(event_dict)


def _redact_mapping(mapping: EventDict) -> EventDict:
    for key, value in list(mapping.items()):
        if key.lower() in _FORBIDDEN_KEYS:
            mapping[key] = _REDACTED
        elif isinstance(value, MutableMapping):
            _redact_mapping(value)
    return mapping


def add_service_context(service: str, instance: str) -> Any:
    """Processor factory stamping the fields that identify this process."""

    def processor(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        event_dict.setdefault("instance", instance)
        return event_dict

    return processor


def configure_logging(
    *,
    level: str = "INFO",
    fmt: str = "json",
    service: str = "agent-forge",
    instance: str = "default",
) -> None:
    """Configure structlog and the stdlib root logger to agree with each other."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        add_service_context(service, instance),
        redact_sensitive,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, httpx, litellm) through the same renderer so the
    # output stream stays parseable.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(processor=renderer, foreign_pre_chain=shared)
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(numeric_level)
    for noisy in ("uvicorn.access", "httpx", "httpcore", "LiteLLM"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Module-level logger.

    The module name is bound as a field rather than added by
    ``structlog.stdlib.add_logger_name``: that processor reads ``logger.name``, which
    only exists on stdlib loggers, and this configuration writes straight to stdout.
    """
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name).bind(logger=name)
    return logger


def bind_request_context(
    *,
    trace_id: str | None = None,
    tenant_id: str | None = None,
    task_id: str | None = None,
    thread_id: str | None = None,
) -> None:
    """Bind correlation ids for the current async context.

    Every log line emitted downstream picks these up automatically, which is what makes
    a single request traceable across graph nodes without threading a logger around.
    """
    values = {
        key: value
        for key, value in (
            ("trace_id", trace_id),
            ("tenant_id", tenant_id),
            ("task_id", task_id),
            ("thread_id", thread_id),
        )
        if value is not None
    }
    if values:
        bind_contextvars(**values)


def clear_request_context() -> None:
    """Drop correlation ids. Call at the end of a request."""
    clear_contextvars()


def unbind_request_keys(*keys: str) -> None:
    """Remove specific correlation ids without clearing the rest."""
    unbind_contextvars(*keys)
