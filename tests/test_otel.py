import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Tracer

from apitally import instrument
from apitally.shared.span_processor import ApitallySpanProcessor
from tests.conftest import unwrap


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture()
def tracer(exporter: InMemorySpanExporter) -> Tracer:
    provider = TracerProvider()
    provider.add_span_processor(ApitallySpanProcessor(SimpleSpanProcessor(exporter)))
    # Global registration so instrument()'s proxy tracer resolves to this provider
    trace.set_tracer_provider(provider)
    return provider.get_tracer("opentelemetry.instrumentation.starlette")


async def test_instrument_creates_child_spans_under_server_span(tracer: Tracer, exporter: InMemorySpanExporter):
    @instrument
    def compute() -> int:
        return 42

    @instrument
    async def compute_async() -> int:
        return 42

    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as server:
        assert compute() == 42
        assert await compute_async() == 42

    children = [s for s in exporter.get_finished_spans() if s.name in ("compute", "compute_async")]
    assert len(children) == 2
    for child in children:
        assert unwrap(child.parent).span_id == server.get_span_context().span_id
