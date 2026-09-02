from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Tracer

from apitally import capture_exception, set_request_attribute
from apitally.shared import server_errors
from apitally.shared.context import get_server_span
from tests.conftest import unwrap


def test_set_request_attribute_targets_server_span(tracer: Tracer, span_exporter: InMemorySpanExporter):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        set_request_attribute("tenant", "acme")
    (server,) = span_exporter.get_finished_spans()
    assert unwrap(server.attributes)["tenant"] == "acme"


def test_set_request_attribute_outside_request_is_silent_noop():
    assert get_server_span() is None
    set_request_attribute("tenant", "acme")


def test_capture_exception_updates_holder_without_recording_span():
    holder = server_errors.init_exception_holder()
    exception = ValueError("x")
    capture_exception(exception)
    assert holder.exception is exception


def test_capture_exception_records_event_on_server_span(tracer: Tracer, span_exporter: InMemorySpanExporter):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        capture_exception(ValueError("x"))
    (server,) = span_exporter.get_finished_spans()
    (event,) = server.events
    assert event.name == "exception"
    assert unwrap(event.attributes)["exception.type"] == "ValueError"
    assert unwrap(event.attributes)["exception.message"] == "x"
    assert "exception.stacktrace" in unwrap(event.attributes)
