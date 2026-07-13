import json
from urllib.parse import parse_qsl

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace import Span as SDKSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import SpanKind

from apitally.shared.config import set_config
from apitally.shared.context import get_server_span_processor
from apitally.shared.exporter import ApitallySpanExporter
from apitally.shared.redaction import REDACTED
from apitally.shared.span_processor import STASH_ATTRIBUTE, ApitallySpanProcessor
from tests.conftest import CONTRIB_SCOPE, WRITE_TOKEN, create_trace_pipeline, unwrap


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


def test_user_attached_exporters_never_see_captured_headers_and_bodies():
    set_config(write_token=WRITE_TOKEN, log_request_headers=True, log_request_body=True)
    user_exporter = InMemorySpanExporter()
    apitally_exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(ApitallySpanProcessor(SimpleSpanProcessor(ApitallySpanExporter(apitally_exporter))))
    provider.add_span_processor(SimpleSpanProcessor(user_exporter))
    tracer = provider.get_tracer(CONTRIB_SCOPE)

    with tracer.start_as_current_span("POST /items", kind=SpanKind.SERVER) as span:
        processor = unwrap(get_server_span_processor())
        processor.update_stash(
            span.get_span_context().span_id,
            request_headers={"authorization": ["Bearer secret123"], "accept": ["application/json"]},
            request_body=b'{"password": "hunter2"}',
        )

    (apitally_span,) = apitally_exporter.get_finished_spans()
    assert json.loads(str(unwrap(apitally_span.attributes)["apitally.request.body"])) == {"password": REDACTED}
    assert unwrap(apitally_span.attributes)["http.request.header.authorization"] == [REDACTED]
    assert unwrap(apitally_span.attributes)["http.request.header.accept"] == ["application/json"]

    (user_span,) = user_exporter.get_finished_spans()
    attributes = dict(user_span.attributes or {})
    assert "apitally.request.body" not in attributes
    assert not any(key.startswith("http.request.header.") for key in attributes)
    assert "hunter2" not in str(attributes)
    assert "secret123" not in str(attributes)
    assert not hasattr(user_span, STASH_ATTRIBUTE)


def test_mask_callback_receives_ended_span():
    seen: list[ReadableSpan] = []

    def mask(span: ReadableSpan, body: bytes) -> bytes:
        seen.append(span)
        return body

    set_config(write_token=WRITE_TOKEN, log_request_headers=True, log_request_body=True, mask_request_body=mask)
    tracer, exporter = create_trace_pipeline()
    with tracer.start_as_current_span("POST /items", kind=SpanKind.SERVER) as span:
        processor = unwrap(get_server_span_processor())
        processor.update_stash(
            span.get_span_context().span_id,
            request_headers={"authorization": ["Bearer secret123"], "content-type": ["application/json"]},
            request_body=b'{"a": 1}',
        )

    (exported,) = exporter.get_finished_spans()
    assert unwrap(exported.attributes)["apitally.request.body"] == '{"a":1}'
    (seen_span,) = seen
    assert seen_span.end_time is not None
    assert unwrap(seen_span.attributes)["http.request.header.authorization"] == [REDACTED]
    assert unwrap(seen_span.attributes)["http.request.header.content-type"] == ["application/json"]
    assert not hasattr(seen_span, STASH_ATTRIBUTE)
