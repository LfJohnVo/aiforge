"""Prometheus metrics, declared once so the label sets cannot drift.

Every metric is defined here rather than at its call site. Two modules that each declare
``agentforge_tool_calls_total`` with a different label set produce a registry error at
import time in the best case, and two incompatible series in the worst -- and the second
one is only noticed when a dashboard goes flat.

Labels are **bounded**. ``tenant``, ``node``, ``model``, ``connector``, ``tool`` all come
from configuration or from a fixed catalogue. Nothing user-supplied ever becomes a label:
a label whose values a caller controls is an unbounded cardinality bomb, and the first
time a user pastes a UUID into one, Prometheus starts eating the host.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST

__all__ = [
    "CONTENT_TYPE_LATEST",
    "Metrics",
    "get_metrics",
    "render",
    "reset_metrics",
]

# Buckets in seconds. The long tail matters more than the short one here: a cell that
# answers in 200 ms and a cell that answers in 400 ms are both fine, and one that takes
# 30 s is the incident.
_LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 40.0, 80.0)
_NODE_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


class Metrics:
    """The cell's metric surface.

    Held as an object rather than module globals so tests can build an isolated registry;
    a metric registered twice in the default registry raises, which would make the second
    test in a module fail for a reason that has nothing to do with what it tests.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()

        self.task_duration = Histogram(
            "agentforge_task_duration_seconds",
            "Wall time of a complete task, from intake to respond",
            ["tenant", "area", "status"],
            buckets=_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.node_duration = Histogram(
            "agentforge_node_duration_seconds",
            "Wall time of one graph node",
            ["tenant", "node"],
            buckets=_NODE_BUCKETS,
            registry=self.registry,
        )
        self.llm_tokens = Counter(
            "agentforge_llm_tokens_total",
            "Tokens consumed, by direction",
            ["tenant", "model", "direction"],
            registry=self.registry,
        )
        self.llm_cost = Counter(
            "agentforge_llm_cost_usd_total",
            "Model spend in USD",
            ["tenant", "model"],
            registry=self.registry,
        )
        self.cache_hits = Counter(
            "agentforge_semantic_cache_hits_total",
            "Semantic cache hits",
            ["tenant"],
            registry=self.registry,
        )
        self.cache_misses = Counter(
            "agentforge_semantic_cache_misses_total",
            "Semantic cache misses",
            ["tenant"],
            registry=self.registry,
        )
        self.tool_calls = Counter(
            "agentforge_tool_calls_total",
            "Tool invocations by outcome",
            ["tenant", "connector", "tool", "outcome"],
            registry=self.registry,
        )
        self.policy_decisions = Counter(
            "agentforge_policy_decisions_total",
            "Policy decisions by kind and effect",
            ["tenant", "decision", "effect"],
            registry=self.registry,
        )
        self.hitl_pending = Gauge(
            "agentforge_hitl_pending",
            "Approvals waiting for a human right now",
            ["tenant"],
            registry=self.registry,
        )
        self.judge_verdicts = Counter(
            "agentforge_judge_verdicts_total",
            "Judge verdicts",
            ["tenant", "verdict"],
            registry=self.registry,
        )
        # A security metric, not a performance one. If it moves, something is trying to
        # send classified data to an external model, and the alert is on the *increase*,
        # not on a threshold.
        self.external_blocked = Counter(
            "agentforge_external_model_blocked_total",
            "Requests refused because their classification may not leave the perimeter",
            ["tenant", "classification"],
            registry=self.registry,
        )
        # Not labelled by caller: the label would be a hash of a credential, which is
        # unbounded cardinality and, worse, a stable identifier for a person in a metrics
        # store that is not access-controlled the way the ledger is.
        self.rate_limited = Counter(
            "agentforge_rate_limited_total",
            "Requests refused because the caller exceeded its per-minute quota",
            ["instance"],
            registry=self.registry,
        )
        # Every graceful degradation in the system increments this. They are the
        # failures that do not look like failures -- the cell keeps answering, worse --
        # so a log line is not enough: without a counter nobody finds out until a user
        # says the answers got vague.
        self.degraded = Counter(
            "agentforge_degraded_total",
            "Times a component answered from a fallback instead of its configured backend",
            ["component"],
            registry=self.registry,
        )
        self.ledger_entries = Counter(
            "agentforge_ledger_entries_total",
            "Records appended to the evidence chain",
            ["tenant", "action"],
            registry=self.registry,
        )
        self.dlp_findings = Counter(
            "agentforge_dlp_findings_total",
            "DLP rule hits by direction and action",
            ["tenant", "rule", "direction", "action"],
            registry=self.registry,
        )
        self.retrieval_results = Histogram(
            "agentforge_retrieval_results",
            "How many chunks a retrieval returned after filtering",
            ["tenant"],
            buckets=(0, 1, 2, 3, 5, 8, 13, 21),
            registry=self.registry,
        )

    # ------------------------------------------------------------------ helpers

    @contextmanager
    def time_node(self, tenant: str, node: str) -> Iterator[None]:
        start = perf_counter()
        try:
            yield
        finally:
            self.node_duration.labels(tenant=tenant, node=node).observe(perf_counter() - start)

    def observe_usage(self, tenant: str, model: str, usage: Any) -> None:
        """Record one model call's tokens and cost."""
        tokens_in = int(getattr(usage, "tokens_in", 0) or 0)
        tokens_out = int(getattr(usage, "tokens_out", 0) or 0)
        cost = float(getattr(usage, "cost_usd", 0.0) or 0.0)
        if tokens_in:
            self.llm_tokens.labels(tenant=tenant, model=model, direction="in").inc(tokens_in)
        if tokens_out:
            self.llm_tokens.labels(tenant=tenant, model=model, direction="out").inc(tokens_out)
        if cost:
            self.llm_cost.labels(tenant=tenant, model=model).inc(cost)

    def render(self) -> bytes:
        return generate_latest(self.registry)


_METRICS: Metrics | None = None


def get_metrics() -> Metrics:
    """The process-wide metrics. Created on first use."""
    global _METRICS
    if _METRICS is None:
        _METRICS = Metrics()
    return _METRICS


def reset_metrics(metrics: Metrics | None = None) -> Metrics:
    """Replace the process-wide metrics. For tests, and for a re-created runtime."""
    global _METRICS
    _METRICS = metrics or Metrics()
    return _METRICS


def render() -> bytes:
    return get_metrics().render()
