"""Observability: spans, metrics and the Langfuse redaction rule.

The phase's first exit criterion is here: a complete request-to-response trace, asserted
as a span tree rather than eyeballed in a UI. The rule that gets the most attention is the
one that is easiest to break by accident -- **a span never carries content** -- because a
span attribute is the shortest path from a classified document to a collector nobody
classified.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_forge.core.classification import Classification
from agent_forge.core.graph import build_graph
from agent_forge.observability import metrics as metrics_module
from agent_forge.observability import tracing
from agent_forge.observability.langfuse_client import LangfuseTracer, build_langfuse
from agent_forge.observability.metrics import Metrics
from tests.support import EXTERNAL, FakeTransport, make_deps, make_gateway, make_state

TENANT = "acme-mx"


@pytest.fixture
def spans() -> Any:
    """A real tracer writing into memory, installed for the duration of one test."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    previous = tracing._TRACER
    tracing._TRACER = provider.get_tracer("test")
    try:
        yield exporter
    finally:
        tracing._TRACER = previous
        provider.shutdown()


@pytest.fixture
def metrics() -> Metrics:
    """An isolated registry: a metric registered twice raises, and would fail the next test."""
    return metrics_module.reset_metrics(Metrics())


# --------------------------------------------------------------------- tracing


async def test_a_full_request_produces_a_span_per_graph_node(spans: Any, metrics: Metrics) -> None:
    """Exit criterion: the whole request-to-response path is visible as one trace."""
    app = build_graph(make_deps(gateway=make_gateway(FakeTransport(replies=["listo"]))))

    await app.ainvoke(make_state("cual es el limite?"))

    names = [s.name for s in spans.get_finished_spans()]
    assert "graph.node.intake" in names
    assert "graph.node.governance_gate" in names
    assert "graph.node.synthesis" in names
    assert "graph.node.respond" in names
    # And the model call inside synthesis, so the trace explains where the time went.
    assert any(n.startswith("llm.") for n in names)


async def test_every_node_span_carries_the_correlation_attributes(
    spans: Any, metrics: Metrics
) -> None:
    app = build_graph(make_deps(gateway=make_gateway(FakeTransport())))

    await app.ainvoke(make_state("hola"))

    node_spans = [s for s in spans.get_finished_spans() if s.name.startswith("graph.node.")]
    assert node_spans
    for span in node_spans:
        assert span.attributes["tenant_id"] == TENANT
        assert span.attributes["task_id"]
        assert span.attributes["classification"]
        assert span.attributes["node"]


async def test_no_span_ever_carries_content(spans: Any, metrics: Metrics) -> None:
    """The rule that matters. Traces leave the perimeter; content must not ride along."""
    secret = "el limite del director general es 90000 MXN"
    app = build_graph(make_deps(gateway=make_gateway(FakeTransport(replies=[secret]))))

    await app.ainvoke(make_state(secret))

    for span in spans.get_finished_spans():
        rendered = " ".join(f"{k}={v}" for k, v in (span.attributes or {}).items())
        assert secret not in rendered
        assert "90000" not in rendered


def test_a_content_attribute_is_dropped_and_reported(spans: Any) -> None:
    """The guard is central because there are dozens of call sites and one slip is enough."""
    with tracing.span("test.span", tenant_id=TENANT, answer="texto secreto", prompt="mas texto"):
        pass

    finished = spans.get_finished_spans()[0]
    assert "answer" not in (finished.attributes or {})
    assert "prompt" not in (finished.attributes or {})
    assert finished.attributes["tenant_id"] == TENANT


def test_an_unknown_attribute_is_dropped_even_when_it_looks_harmless(spans: Any) -> None:
    """An allowlist, not a denylist: a new attribute is refused until someone adds it."""
    with tracing.span("test.span", tenant_id=TENANT, customer_reference="ACME-9931"):
        pass

    assert "customer_reference" not in (spans.get_finished_spans()[0].attributes or {})


def test_an_exception_marks_the_span_without_quoting_the_input(spans: Any) -> None:
    with pytest.raises(ValueError), tracing.span("test.span", tenant_id=TENANT):
        raise ValueError("el limite del director es 90000")

    finished = spans.get_finished_spans()[0]
    assert finished.status.status_code is trace.StatusCode.ERROR
    assert "90000" not in (finished.status.description or "")


def test_tracing_is_a_no_op_when_no_endpoint_is_configured() -> None:
    """A laptop runs the same instrumented code as a cluster, with nothing installed."""
    tracing.shutdown_tracing()

    assert tracing.setup_tracing(endpoint="") is None
    with tracing.span("test.span", tenant_id=TENANT) as current:
        assert current is None
    assert tracing.current_trace_id() == ""


# --------------------------------------------------------------------- metrics


async def test_a_run_records_a_duration_for_every_node(metrics: Metrics) -> None:
    app = build_graph(make_deps(gateway=make_gateway(FakeTransport())))

    await app.ainvoke(make_state("hola"))

    rendered = metrics.render().decode()
    assert 'agentforge_node_duration_seconds_count{node="intake"' in rendered
    assert 'node="synthesis"' in rendered


async def test_model_usage_lands_in_the_token_and_cost_counters(metrics: Metrics) -> None:
    gateway = make_gateway(FakeTransport(replies=["respuesta"]))

    await gateway.complete(
        [{"role": "user", "content": "hola"}],
        classification=Classification.C0,
        preferred=["local/fast"],
        tenant_id=TENANT,
    )

    rendered = metrics.render().decode()
    assert "agentforge_llm_tokens_total" in rendered
    assert f'tenant="{TENANT}"' in rendered


async def test_a_refused_external_route_increments_the_security_counter(
    metrics: Metrics,
) -> None:
    """`agentforge_external_model_blocked_total` moving is how an operator learns."""
    from agent_forge.core.errors import SovereigntyError

    gateway = make_gateway(FakeTransport(), backends=[EXTERNAL])

    with pytest.raises(SovereigntyError):
        await gateway.complete(
            [{"role": "user", "content": "expediente"}],
            classification=Classification.C4,
            preferred=[EXTERNAL.alias],
            tenant_id=TENANT,
        )

    rendered = metrics.render().decode()
    assert "agentforge_external_model_blocked_total" in rendered
    assert 'classification="C4"' in rendered


async def test_the_judge_verdict_is_counted(metrics: Metrics) -> None:
    from agent_forge.core.state import QualityVerdict

    async def judge(state: Any) -> QualityVerdict:
        return QualityVerdict(verdict="approve", scores={"coverage": 1.0})

    app = build_graph(make_deps(gateway=make_gateway(FakeTransport()), quality=judge))

    await app.ainvoke(make_state("hola"))

    assert 'agentforge_judge_verdicts_total{tenant="acme-mx",verdict="approve"}' in (
        metrics.render().decode()
    )


def test_no_metric_label_is_caller_controlled() -> None:
    """A label a user can set is an unbounded cardinality bomb."""
    isolated = Metrics()

    for collector in isolated.registry.collect():
        for sample in collector.samples:
            for label in sample.labels:
                assert label in {
                    "tenant",
                    "area",
                    "status",
                    "node",
                    "model",
                    "direction",
                    "connector",
                    "tool",
                    "outcome",
                    "decision",
                    "effect",
                    "verdict",
                    "classification",
                    "action",
                    "rule",
                    "le",
                    "quantile",
                }, label


def test_the_metrics_endpoint_serves_the_prometheus_format(metrics: Metrics) -> None:
    metrics.hitl_pending.labels(tenant=TENANT).set(3)

    body = metrics.render().decode()

    assert "# HELP agentforge_hitl_pending" in body
    assert f'agentforge_hitl_pending{{tenant="{TENANT}"}} 3.0' in body


# -------------------------------------------------------------------- langfuse


def test_langfuse_is_disabled_without_keys() -> None:
    """A profile enabling it is not the same as an operator having provisioned it."""
    tracer = build_langfuse(public_key="", secret_key="", enabled=True)

    assert tracer.enabled is False
    tracer.trace_task(make_state("hola"))  # a no-op, not a crash


def test_confidential_content_is_digested_before_it_reaches_langfuse() -> None:
    """Langfuse is hosted by default. C2 and above travel as digests, not as text."""
    captured: list[dict[str, Any]] = []
    tracer = LangfuseTracer(client=_CapturingClient(captured))
    state = make_state("cual es el limite?", ceiling=Classification.C2).model_copy(
        update={"answer": "El limite es 1500 MXN.", "classification": Classification.C2}
    )

    tracer.trace_task(state)

    payload = captured[0]
    assert "1500" not in str(payload["output"])
    assert payload["output"]["digest"]
    assert payload["metadata"]["redacted"] is True


def test_public_content_reaches_langfuse_in_full() -> None:
    """Otherwise the tool shows nothing useful, which is the other failure mode."""
    captured: list[dict[str, Any]] = []
    tracer = LangfuseTracer(client=_CapturingClient(captured))
    state = make_state("hola", ceiling=Classification.C0).model_copy(
        update={"answer": "Hola, en que te ayudo?", "classification": Classification.C0}
    )

    tracer.trace_task(state)

    assert captured[0]["output"] == "Hola, en que te ayudo?"
    assert captured[0]["metadata"]["redacted"] is False


def test_an_unparseable_classification_is_treated_as_secret() -> None:
    captured: list[dict[str, Any]] = []
    tracer = LangfuseTracer(client=_CapturingClient(captured))

    tracer.trace_task(_Bogus())

    assert captured[0]["metadata"]["redacted"] is True


def test_a_langfuse_outage_does_not_break_the_request() -> None:
    class _Broken:
        def start_observation(self, **kwargs: Any) -> Any:
            raise ConnectionError("langfuse is down")

    LangfuseTracer(client=_Broken()).trace_task(make_state("hola"))


class _CapturingClient:
    def __init__(self, sink: list[dict[str, Any]]) -> None:
        self._sink = sink

    def start_observation(self, **kwargs: Any) -> Any:
        self._sink.append(kwargs)

        class _Observation:
            def end(self) -> None:
                return None

        return _Observation()

    def create_score(self, **kwargs: Any) -> None:
        return None

    def flush(self) -> None:
        return None


class _Bogus:
    classification = "NOT-A-LEVEL"
    answer = "algo"
    last_user_message = "algo"
    area = "finanzas"
    task_id = "t1"
    thread_id = "th1"
    autonomy = "A0"
    citations: tuple[Any, ...] = ()
    retries = 0
    judge_scores: ClassVar[dict[str, float]] = {}
    identity: Any = None

    class usage:  # noqa: N801 - a stand-in for the Usage model
        tokens_in = 0
        tokens_out = 0
        cost_usd = 0.0
