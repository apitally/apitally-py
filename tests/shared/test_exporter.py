from urllib.parse import parse_qsl

from opentelemetry.sdk.trace import Span as SDKSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from apitally.shared.exporter import ApitallySpanExporter
from apitally.shared.redaction import REDACTED
from tests.conftest import create_trace_pipeline, unwrap


def test_forwarded_span_redacted_and_original_unmutated():
    tracer, exporter = create_trace_pipeline()
    attributes = {
        "url.query": "token=secret123&page=2",
        "http.target": "/items?token=secret123&page=2",
        "http.url": "https://example.com/items?token=secret123&page=2",
        "http.request.header.authorization": ("Bearer secret123",),
        "http.request.header.x_api_key": ("secret123",),
        "http.response.header.set-cookie": ("session=abc",),
        "http.response.header.content-type": ("application/json",),
    }
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER, attributes=attributes) as span:
        pass
    assert isinstance(span, SDKSpan)

    (exported,) = exporter.get_finished_spans()
    assert dict(parse_qsl(str(unwrap(exported.attributes)["url.query"]))) == {"token": REDACTED, "page": "2"}
    assert dict(parse_qsl(str(unwrap(exported.attributes)["http.target"]).partition("?")[2])) == {
        "token": REDACTED,
        "page": "2",
    }
    assert "secret123" not in str(unwrap(exported.attributes)["http.url"])
    assert unwrap(exported.attributes)["http.request.header.authorization"] == [REDACTED]
    assert unwrap(exported.attributes)["http.request.header.x_api_key"] == [REDACTED]
    assert unwrap(exported.attributes)["http.response.header.set-cookie"] == [REDACTED]
    assert unwrap(exported.attributes)["http.response.header.content-type"] == ("application/json",)

    assert unwrap(span.attributes)["url.query"] == "token=secret123&page=2"
    assert unwrap(span.attributes)["http.request.header.authorization"] == ("Bearer secret123",)


def test_client_span_url_full_redacted():
    tracer, exporter = create_trace_pipeline()
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        with tracer.start_as_current_span(
            "GET", kind=SpanKind.CLIENT, attributes={"url.full": "https://x.example/v1?api-key=secret&ok=1"}
        ):
            pass
    client = next(s for s in exporter.get_finished_spans() if s.kind == SpanKind.CLIENT)
    url = str(unwrap(client.attributes)["url.full"])
    assert url.startswith("https://x.example/v1?")
    assert dict(parse_qsl(url.partition("?")[2])) == {"api-key": REDACTED, "ok": "1"}


def test_span_without_sensitive_attributes_passes_through_unchanged():
    tracer, exporter = create_trace_pipeline()
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER, attributes={"url.path": "/items"}):
        pass
    (span,) = exporter.get_finished_spans()

    delegate = InMemorySpanExporter()
    ApitallySpanExporter(delegate).export([span])
    (passed_through,) = delegate.get_finished_spans()
    assert passed_through is span
