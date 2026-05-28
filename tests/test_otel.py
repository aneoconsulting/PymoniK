"""OTel integration.

Two regimes to verify:

1. **OTel not installed / disabled** — every helper is a no-op, the
   submission pipeline runs unchanged. The most important property is
   "pymonik works without ``opentelemetry-api`` even being importable",
   which is hard to test from inside a process where it *is* importable
   — we settle for "all helpers no-op when ``setup(force=False)``".

2. **OTel installed and enabled** — spans are produced for the documented
   call sites; the worker can extract the trace context the client put
   in the envelope; nesting works.
"""

from __future__ import annotations

import pytest

from pymonik import task
from pymonik._internal import _otel
from pymonik.envelope import TaskEnvelope, decode, encode

# Skip when the OTel API isn't installed — the no-op behaviour is exercised
# by every other test in the suite (which run with otel disabled).
pytest.importorskip("opentelemetry")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry import trace


@pytest.fixture(scope="module")
def _provider_and_exporter():
    """OTel only allows one TracerProvider per process; install once."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _otel._initialised = False
    _otel._enabled = False
    _otel.setup(force=True)
    yield exporter


@pytest.fixture
def in_memory_exporter(_provider_and_exporter):
    """Per-test handle. Clears spans between tests."""
    _provider_and_exporter.clear()
    yield _provider_and_exporter


def test_no_otel_helpers_are_noop_when_disabled():
    """Force-disabled state: start_span yields None, inject is no-op.

    Run BEFORE the in_memory_exporter fixture installs a provider — once
    installed it's irreversible per process. We achieve that by ordering
    via name (this test starts with "test_no_otel" → first lex order).
    """
    saved_init, saved_enabled = _otel._initialised, _otel._enabled
    _otel._initialised = False
    _otel._enabled = False
    try:
        _otel.setup(force=False)
        with _otel.start_span("pymonik.test") as span:
            assert span is None
        carrier: dict[str, str] = {}
        _otel.inject_context(carrier)
        assert carrier == {}
        assert _otel.current_trace_id_hex() is None
    finally:
        _otel._initialised, _otel._enabled = saved_init, saved_enabled


def test_start_span_emits_attributes(in_memory_exporter):
    with _otel.start_span(
        "pymonik.test", attrs={"pymonik.thing": 42}, kind="client"
    ) as span:
        assert span is not None
    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    s = spans[0]
    assert s.name == "pymonik.test"
    assert s.attributes is not None
    assert s.attributes.get("pymonik.thing") == 42


def test_inject_extract_round_trip(in_memory_exporter):
    """Client injects trace context into a carrier; worker-equivalent
    extracts it and a span opened there is a child of the original."""
    with _otel.start_span("client.parent") as parent:
        carrier: dict[str, str] = {}
        _otel.inject_context(carrier)
        assert "traceparent" in carrier  # W3C default propagator key

    # Simulate the worker: clear the current span, then re-attach via
    # the carrier and open a child span.
    with _otel.use_extracted_context(carrier):
        with _otel.start_span("worker.child") as child:
            assert child is not None

    spans = {s.name: s for s in in_memory_exporter.get_finished_spans()}
    assert "client.parent" in spans
    assert "worker.child" in spans
    # The child's parent span context matches the parent's span id.
    parent_span = spans["client.parent"]
    child_span = spans["worker.child"]
    assert child_span.parent is not None
    assert child_span.parent.trace_id == parent_span.context.trace_id
    assert child_span.parent.span_id == parent_span.context.span_id


def test_envelope_carries_otel_context_round_trip():
    env = TaskEnvelope(
        function_pickle=b"f",
        args_pickle=b"a",
        func_name="t",
        otel_context=(("traceparent", "00-abcd-ef01-01"),),
    )
    rt = decode(encode(env))
    assert rt.otel_context == (("traceparent", "00-abcd-ef01-01"),)


def test_submit_pipeline_injects_context(in_memory_exporter):
    """End-to-end: submit_many through LocalCluster → worker dispatch.
    Verify the envelope produced carries trace context, the worker-side
    span is a child of the client-side submit span."""
    from pymonik.testing import LocalCluster

    @task
    def echo(x: int) -> int:
        return x

    with LocalCluster() as client:
        with client.session() as s:
            assert echo.spawn(7).result(timeout=15) == 7

    spans_by_name = {s.name: s for s in in_memory_exporter.get_finished_spans()}
    assert "pymonik.submit" in spans_by_name
    assert "pymonik.task.dispatch" in spans_by_name
    assert "pymonik.task.run" in spans_by_name
    submit = spans_by_name["pymonik.submit"]
    dispatch = spans_by_name["pymonik.task.dispatch"]
    run = spans_by_name["pymonik.task.run"]
    # All three share one trace.
    assert run.context.trace_id == submit.context.trace_id == dispatch.context.trace_id
    # Hierarchy: submit (client) -> task.dispatch (worker) -> task.run (user fn).
    assert dispatch.parent is not None
    assert dispatch.parent.span_id == submit.context.span_id
    assert run.parent is not None
    assert run.parent.span_id == dispatch.context.span_id


def test_submit_span_has_useful_attributes(in_memory_exporter):
    from pymonik.testing import LocalCluster

    @task
    def add(a: int, b: int) -> int:
        return a + b

    with LocalCluster() as client:
        with client.session(partition="cpu") as s:
            add.map(range(4), range(1, 5)).results(timeout=15)

    submit_spans = [
        s for s in in_memory_exporter.get_finished_spans() if s.name == "pymonik.submit"
    ]
    assert len(submit_spans) == 1
    attrs = submit_spans[0].attributes
    assert attrs is not None
    assert attrs.get("pymonik.func") == "add"
    assert attrs.get("pymonik.count") == 4
    assert attrs.get("pymonik.partition") == "cpu"
