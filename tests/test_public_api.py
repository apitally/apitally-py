import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from apitally import capture_exception, instrument, set_consumer, set_request_attribute
from apitally.shared.consumer import consumer_identifier_var, get_consumer_identifier
from apitally.shared.span_processor import ApitallySpanProcessor, get_server_span, server_span_var


@pytest.fixture(autouse=True)
def reset_context_vars():
    yield
    server_span_var.set(None)
    consumer_identifier_var.set(None)


@pytest.fixture()
def exporter():
    return InMemorySpanExporter()


@pytest.fixture()
def tracer(exporter):
    provider = TracerProvider()
    provider.add_span_processor(ApitallySpanProcessor(SimpleSpanProcessor(exporter)))
    # Global registration so instrument()'s proxy tracer resolves to this provider;
    # the conftest autouse fixture resets trace globals after each test
    trace.set_tracer_provider(provider)
    return provider.get_tracer("opentelemetry.instrumentation.starlette")


def test_set_consumer_targets_server_span_from_child_span(tracer, exporter):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        with tracer.start_as_current_span("child"):
            set_consumer(" acme-corp ", name=" Acme Corp ", group="enterprise")
    child, server = exporter.get_finished_spans()
    assert server.attributes["apitally.consumer.identifier"] == "acme-corp"
    assert server.attributes["apitally.consumer.name"] == "Acme Corp"
    assert server.attributes["apitally.consumer.group"] == "enterprise"
    assert not any(key.startswith("apitally.consumer.") for key in child.attributes)
    assert get_consumer_identifier() == "acme-corp"


def test_set_consumer_truncates_identifier_name_and_group(tracer, exporter):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        set_consumer("i" * 200, name="n" * 100, group="g" * 100)
    (server,) = exporter.get_finished_spans()
    assert server.attributes["apitally.consumer.identifier"] == "i" * 128
    assert server.attributes["apitally.consumer.name"] == "n" * 64
    assert server.attributes["apitally.consumer.group"] == "g" * 64


def test_set_request_attribute_outside_request_is_silent_noop():
    assert get_server_span() is None
    set_request_attribute("tenant", "acme")


def test_capture_exception_records_event_on_server_span(tracer, exporter):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        capture_exception(ValueError("x"))
    (server,) = exporter.get_finished_spans()
    (event,) = server.events
    assert event.name == "exception"
    assert event.attributes["exception.type"] == "ValueError"
    assert event.attributes["exception.message"] == "x"
    assert "exception.stacktrace" in event.attributes


def test_capture_exception_with_non_exception_does_not_raise(tracer):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        capture_exception("not an exception")  # ty: ignore[invalid-argument-type]


def test_instrument_creates_child_span_under_server_span(tracer, exporter):
    @instrument
    def compute():
        return 42

    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as server:
        assert compute() == 42
    child = next(s for s in exporter.get_finished_spans() if s.name == "compute")
    assert child.parent.span_id == server.context.span_id


def test_writes_to_excluded_request_stay_local(tracer, exporter):
    with tracer.start_as_current_span("GET /healthz", kind=SpanKind.SERVER, attributes={"url.path": "/healthz"}):
        set_consumer("acme-corp")
        set_request_attribute("tenant", "acme")
        capture_exception(ValueError("x"))
    assert exporter.get_finished_spans() == ()
    assert get_consumer_identifier() == "acme-corp"
