import contextvars
import logging
from urllib.parse import parse_qsl

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, TraceFlags

from apitally.shared.config import configure
from apitally.shared.redaction import REDACTED
from apitally.shared.span_processor import ApitallySpanProcessor, get_server_span, server_span_var


TOKEN = "apt_" + "a" * 24
CONTRIB_SCOPE = "opentelemetry.instrumentation.starlette"


@pytest.fixture(autouse=True)
def reset_server_span_var():
    # The var intentionally persists after a request ends (design.md section 5), so tests
    # sharing one context must clear it between runs
    yield
    server_span_var.set(None)


@pytest.fixture()
def exporter():
    return InMemorySpanExporter()


@pytest.fixture()
def processor(exporter):
    return ApitallySpanProcessor(SimpleSpanProcessor(exporter))


@pytest.fixture()
def tracer_provider(processor):
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(processor)
    return provider


@pytest.fixture()
def tracer(tracer_provider):
    return tracer_provider.get_tracer(CONTRIB_SCOPE)


def test_server_root_and_child_kept(tracer, processor, exporter):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as server:
        with tracer.start_as_current_span("child") as child:
            assert processor.resolve_server_span_id(child.context.span_id) == server.context.span_id
    assert {s.name for s in exporter.get_finished_spans()} == {"GET /items", "child"}
    assert not processor.spans


def test_server_span_with_unsampled_remote_parent_kept(tracer, exporter):
    remote = SpanContext(trace_id=1, span_id=2, is_remote=True, trace_flags=TraceFlags(TraceFlags.DEFAULT))
    context = trace.set_span_in_context(NonRecordingSpan(remote))
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER, context=context):
        pass
    assert len(exporter.get_finished_spans()) == 1


def test_non_server_root_and_children_dropped(tracer, exporter):
    with tracer.start_as_current_span("background job"):
        with tracer.start_as_current_span("child"):
            pass
    assert exporter.get_finished_spans() == ()


def test_options_request_dropped(tracer, exporter):
    attributes = {"http.request.method": "OPTIONS", "url.path": "/items"}
    with tracer.start_as_current_span("OPTIONS /items", kind=SpanKind.SERVER, attributes=attributes):
        pass
    assert exporter.get_finished_spans() == ()


@pytest.mark.parametrize(
    "attributes",
    [
        {"http.request.method": "GET", "url.path": "/healthz"},
        {"http.request.method": "GET", "url.path": "/", "user_agent.original": "kube-probe/1.30"},
        {"http.method": "GET", "http.target": "/healthz?full=1"},
        {"http.method": "GET", "http.target": "/", "http.user_agent": "kube-probe/1.30"},
    ],
)
def test_default_excluded_requests_dropped(tracer, exporter, attributes):
    with tracer.start_as_current_span("GET", kind=SpanKind.SERVER, attributes=attributes):
        pass
    assert exporter.get_finished_spans() == ()


def test_user_exclude_paths_add_to_defaults(tracer, exporter):
    configure(write_token=TOKEN, exclude_paths=[r"^/internal/"])
    for path in ["/internal/jobs", "/healthz", "/items"]:
        with tracer.start_as_current_span(f"GET {path}", kind=SpanKind.SERVER, attributes={"url.path": path}):
            pass
    assert [s.name for s in exporter.get_finished_spans()] == ["GET /items"]


def test_exclude_on_request_callback(tracer, exporter):
    configure(write_token=TOKEN, exclude_on_request=lambda span: span.attributes.get("url.path") == "/admin")
    with tracer.start_as_current_span("GET /admin", kind=SpanKind.SERVER, attributes={"url.path": "/admin"}):
        with tracer.start_as_current_span("child"):
            pass
    assert exporter.get_finished_spans() == ()


def test_raising_exclude_on_request_warns_and_keeps(tracer, exporter, caplog):
    def callback(span):
        raise ValueError("boom")

    configure(write_token=TOKEN, exclude_on_request=callback)
    with caplog.at_level(logging.WARNING, logger="apitally"):
        with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
            pass
    assert len(exporter.get_finished_spans()) == 1
    assert any("exclude_on_request" in r.getMessage() for r in caplog.records)


def test_exclude_on_response_callback(tracer, exporter):
    configure(
        write_token=TOKEN,
        exclude_on_response=lambda span: span.attributes.get("http.response.status_code") == 404,
    )
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as span:
        span.set_attribute("http.response.status_code", 404)
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as span:
        span.set_attribute("http.response.status_code", 200)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["http.response.status_code"] == 200


def test_contrib_send_receive_spans_dropped_user_spans_kept(tracer_provider, tracer, exporter):
    user_tracer = tracer_provider.get_tracer("myapp")
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        with tracer.start_as_current_span("GET /items http receive"):
            pass
        with tracer.start_as_current_span("GET /items http send"):
            pass
        with user_tracer.start_as_current_span("my http send"):
            pass
    assert {s.name for s in exporter.get_finished_spans()} == {"GET /items", "my http send"}


def test_forwarded_span_redacted_and_original_unmutated(tracer, exporter):
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

    (exported,) = exporter.get_finished_spans()
    assert dict(parse_qsl(str(exported.attributes["url.query"]))) == {"token": REDACTED, "page": "2"}
    assert dict(parse_qsl(str(exported.attributes["http.target"]).partition("?")[2])) == {
        "token": REDACTED,
        "page": "2",
    }
    assert "secret123" not in str(exported.attributes["http.url"])
    assert exported.attributes["http.request.header.authorization"] == [REDACTED]
    assert exported.attributes["http.request.header.x_api_key"] == [REDACTED]
    assert exported.attributes["http.response.header.set-cookie"] == [REDACTED]
    assert exported.attributes["http.response.header.content-type"] == ("application/json",)

    assert span.attributes["url.query"] == "token=secret123&page=2"
    assert span.attributes["http.request.header.authorization"] == ("Bearer secret123",)


def test_client_span_url_full_redacted(tracer, exporter):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        with tracer.start_as_current_span(
            "GET", kind=SpanKind.CLIENT, attributes={"url.full": "https://x.example/v1?api-key=secret&ok=1"}
        ):
            pass
    client = next(s for s in exporter.get_finished_spans() if s.kind == SpanKind.CLIENT)
    url = str(client.attributes["url.full"])
    assert url.startswith("https://x.example/v1?")
    assert dict(parse_qsl(url.partition("?")[2])) == {"api-key": REDACTED, "ok": "1"}


def test_context_var_resolves_server_span_inside_request_only(tracer):
    def handle_request():
        with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as server:
            with tracer.start_as_current_span("child"):
                assert get_server_span() is server

    assert get_server_span() is None
    contextvars.copy_context().run(handle_request)
    assert get_server_span() is None
