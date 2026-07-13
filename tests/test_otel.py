import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Tracer

from apitally import instrument, span
from apitally.shared.span_processor import ApitallySpanProcessor
from tests.conftest import CONTRIB_SCOPE, unwrap


@pytest.fixture()
def tracer(span_exporter: InMemorySpanExporter) -> Tracer:
    provider = TracerProvider()
    provider.add_span_processor(ApitallySpanProcessor(SimpleSpanProcessor(span_exporter)))
    # Global registration so instrument()'s proxy tracer resolves to this provider
    trace.set_tracer_provider(provider)
    return provider.get_tracer(CONTRIB_SCOPE)


async def test_instrument_creates_child_spans_under_server_span(tracer: Tracer, span_exporter: InMemorySpanExporter):
    @instrument
    def compute() -> int:
        return 42

    @instrument
    async def compute_async() -> int:
        return 42

    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as server:
        assert compute() == 42
        assert await compute_async() == 42

    children = [s for s in span_exporter.get_finished_spans() if s.name in ("compute", "compute_async")]
    assert len(children) == 2
    for child in children:
        assert unwrap(child.parent).span_id == server.get_span_context().span_id


def test_span_creates_child_span_under_server_span(tracer: Tracer, span_exporter: InMemorySpanExporter):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as server:
        with span("compute", attributes={"step": "one"}) as current:
            assert current.is_recording()

    (child,) = [s for s in span_exporter.get_finished_spans() if s.name == "compute"]
    assert unwrap(child.parent).span_id == server.get_span_context().span_id
    assert unwrap(child.attributes)["step"] == "one"
