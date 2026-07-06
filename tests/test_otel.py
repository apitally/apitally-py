from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Tracer

from apitally import instrument
from apitally.shared.span_processor import ApitallySpanProcessor, server_span_var
from tests.conftest import unwrap


@pytest.fixture(autouse=True)
def reset_context_vars() -> Iterator[None]:
    yield
    server_span_var.set(None)


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture()
def tracer(exporter: InMemorySpanExporter) -> Tracer:
    provider = TracerProvider()
    provider.add_span_processor(ApitallySpanProcessor(SimpleSpanProcessor(exporter)))
    # Global registration so instrument()'s proxy tracer resolves to this provider;
    # the conftest autouse fixture resets trace globals after each test
    trace.set_tracer_provider(provider)
    return provider.get_tracer("opentelemetry.instrumentation.starlette")


def test_instrument_creates_child_span_under_server_span(tracer: Tracer, exporter: InMemorySpanExporter):
    @instrument
    def compute() -> int:
        return 42

    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as server:
        assert compute() == 42
    child = next(s for s in exporter.get_finished_spans() if s.name == "compute")
    assert unwrap(child.parent).span_id == server.get_span_context().span_id
