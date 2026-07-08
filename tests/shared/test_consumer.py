from contextvars import copy_context

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Tracer

from apitally import capture_exception, set_consumer, set_request_attribute
from apitally.shared.consumer import get_consumer_identifier, reset_consumer
from apitally.shared.span_processor import ApitallySpanProcessor, get_server_span
from tests.conftest import unwrap


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture()
def tracer(exporter: InMemorySpanExporter) -> Tracer:
    provider = TracerProvider()
    provider.add_span_processor(ApitallySpanProcessor(SimpleSpanProcessor(exporter)))
    return provider.get_tracer("opentelemetry.instrumentation.starlette")


def test_set_consumer_targets_server_span_from_child_span(tracer: Tracer, exporter: InMemorySpanExporter):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        with tracer.start_as_current_span("child"):
            set_consumer(" acme-corp ", name=" Acme Corp ", group="enterprise")
    child, server = exporter.get_finished_spans()
    assert unwrap(server.attributes)["apitally.consumer.identifier"] == "acme-corp"
    assert unwrap(server.attributes)["apitally.consumer.name"] == "Acme Corp"
    assert unwrap(server.attributes)["apitally.consumer.group"] == "enterprise"
    assert not any(key.startswith("apitally.consumer.") for key in unwrap(child.attributes))
    assert get_consumer_identifier() == "acme-corp"


def test_set_consumer_truncates_identifier_name_and_group(tracer: Tracer, exporter: InMemorySpanExporter):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        set_consumer("i" * 200, name="n" * 100, group="g" * 100)
    (server,) = exporter.get_finished_spans()
    assert unwrap(server.attributes)["apitally.consumer.identifier"] == "i" * 128
    assert unwrap(server.attributes)["apitally.consumer.name"] == "n" * 64
    assert unwrap(server.attributes)["apitally.consumer.group"] == "g" * 64


def test_consumer_set_in_copied_context_without_span_visible_from_parent_context():
    # Sync endpoints (anyio threadpool) and BaseHTTPMiddleware child tasks run in copied
    # contexts; the shared holder must carry the identifier back even with no recording span
    reset_consumer()
    copy_context().run(set_consumer, "acme-corp")
    assert get_server_span() is None
    assert get_consumer_identifier() == "acme-corp"


def test_writes_to_excluded_request_stay_local(tracer: Tracer, exporter: InMemorySpanExporter):
    with tracer.start_as_current_span("GET /healthz", kind=SpanKind.SERVER, attributes={"url.path": "/healthz"}):
        set_consumer("acme-corp")
        set_request_attribute("tenant", "acme")
        capture_exception(ValueError("x"))
    assert exporter.get_finished_spans() == ()
    assert get_consumer_identifier() == "acme-corp"
