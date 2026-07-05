import contextvars
import logging
from collections.abc import Iterator
from urllib.parse import parse_qsl

import pytest
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace import Span as SDKSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, TraceIdRatioBased
from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, TraceFlags, Tracer

from apitally.shared.config import configure
from apitally.shared.redaction import REDACTED
from apitally.shared.span_processor import (
    ApitallySpanProcessor,
    get_server_span,
    is_server_span_kept,
    server_span_kept_var,
    server_span_var,
)
from tests.conftest import unwrap


TOKEN = "apt_" + "a" * 24
CONTRIB_SCOPE = "opentelemetry.instrumentation.starlette"
BOUND_HALF = TraceIdRatioBased.get_bound_for_rate(0.5)


def remote_parent_context(trace_id: int) -> Context:
    remote = SpanContext(trace_id=trace_id, span_id=1, is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED))
    return trace.set_span_in_context(NonRecordingSpan(remote))


@pytest.fixture(autouse=True)
def reset_server_span_var() -> Iterator[None]:
    # The vars intentionally persist after a request ends (design.md section 5), so tests
    # sharing one context must clear them between runs
    yield
    server_span_var.set(None)
    server_span_kept_var.set(False)


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture()
def processor(exporter: InMemorySpanExporter) -> ApitallySpanProcessor:
    return ApitallySpanProcessor(SimpleSpanProcessor(exporter))


@pytest.fixture()
def tracer_provider(processor: ApitallySpanProcessor) -> TracerProvider:
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(processor)
    return provider


@pytest.fixture()
def tracer(tracer_provider: TracerProvider) -> Tracer:
    return tracer_provider.get_tracer(CONTRIB_SCOPE)


def create_tracer(exporter: SpanExporter) -> Tracer:
    # For tests that configure() in the body: the processor binds config at construction,
    # so it must be built after configure, not in a fixture
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(ApitallySpanProcessor(SimpleSpanProcessor(exporter)))
    return provider.get_tracer(CONTRIB_SCOPE)


def test_server_root_and_child_kept(tracer: Tracer, processor: ApitallySpanProcessor, exporter: InMemorySpanExporter):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as server:
        with tracer.start_as_current_span("child") as child:
            assert (
                processor.resolve_server_span_id(child.get_span_context().span_id) == server.get_span_context().span_id
            )
    assert {s.name for s in exporter.get_finished_spans()} == {"GET /items", "child"}
    assert not processor.spans


def test_server_span_with_unsampled_remote_parent_kept(tracer: Tracer, exporter: InMemorySpanExporter):
    remote = SpanContext(trace_id=1, span_id=2, is_remote=True, trace_flags=TraceFlags(TraceFlags.DEFAULT))
    context = trace.set_span_in_context(NonRecordingSpan(remote))
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER, context=context):
        pass
    assert len(exporter.get_finished_spans()) == 1


def test_non_server_root_and_children_dropped(tracer: Tracer, exporter: InMemorySpanExporter):
    with tracer.start_as_current_span("background job"):
        with tracer.start_as_current_span("child"):
            pass
    assert exporter.get_finished_spans() == ()


def test_options_request_dropped(tracer: Tracer, exporter: InMemorySpanExporter):
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
def test_default_excluded_requests_dropped(tracer: Tracer, exporter: InMemorySpanExporter, attributes: dict[str, str]):
    with tracer.start_as_current_span("GET", kind=SpanKind.SERVER, attributes=attributes):
        pass
    assert exporter.get_finished_spans() == ()


def test_user_exclude_paths_add_to_defaults(exporter: InMemorySpanExporter):
    configure(write_token=TOKEN, exclude_paths=[r"^/internal/"])
    tracer = create_tracer(exporter)
    for path in ["/internal/jobs", "/healthz", "/items"]:
        with tracer.start_as_current_span(f"GET {path}", kind=SpanKind.SERVER, attributes={"url.path": path}):
            pass
    assert [s.name for s in exporter.get_finished_spans()] == ["GET /items"]


def test_sample_rate_deterministic_by_trace_id(exporter: InMemorySpanExporter):
    configure(write_token=TOKEN, sample_rate=0.5)
    tracer = create_tracer(exporter)
    for trace_id in (BOUND_HALF - 1, BOUND_HALF):
        with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER, context=remote_parent_context(trace_id)):
            pass
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert unwrap(spans[0].context).trace_id == BOUND_HALF - 1


def test_sample_on_request_bool_overrides_sample_rate(exporter: InMemorySpanExporter):
    configure(
        write_token=TOKEN,
        sample_rate=0.0,
        sample_on_request=lambda span: span.attributes.get("url.path") != "/bots",
    )
    tracer = create_tracer(exporter)
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER, attributes={"url.path": "/items"}):
        pass
    with tracer.start_as_current_span("GET /bots", kind=SpanKind.SERVER, attributes={"url.path": "/bots"}):
        with tracer.start_as_current_span("child"):
            pass
    assert [s.name for s in exporter.get_finished_spans()] == ["GET /items"]


def test_sample_on_request_float_with_none_fallback(exporter: InMemorySpanExporter):
    bound_low = TraceIdRatioBased.get_bound_for_rate(0.01)
    configure(
        write_token=TOKEN,
        sample_rate=0.5,
        sample_on_request=lambda span: 0.01 if span.attributes.get("url.path") == "/noisy" else None,
    )
    tracer = create_tracer(exporter)
    for path, trace_id, kept in [
        ("/noisy", bound_low - 1, True),
        ("/noisy", bound_low, False),
        ("/other", BOUND_HALF - 1, True),
        ("/other", BOUND_HALF, False),
    ]:
        exporter.clear()
        with tracer.start_as_current_span(
            f"GET {path}", kind=SpanKind.SERVER, context=remote_parent_context(trace_id), attributes={"url.path": path}
        ):
            pass
        assert bool(exporter.get_finished_spans()) is kept, (path, trace_id)


def test_excluded_request_never_invokes_sample_callback(exporter: InMemorySpanExporter):
    calls: list[ReadableSpan] = []

    def callback(span: ReadableSpan) -> None:
        calls.append(span)

    configure(write_token=TOKEN, sample_on_request=callback)
    tracer = create_tracer(exporter)
    with tracer.start_as_current_span("GET /healthz", kind=SpanKind.SERVER, attributes={"url.path": "/healthz"}):
        pass
    assert not calls
    assert exporter.get_finished_spans() == ()


def test_raising_sample_on_request_warns_and_keeps(exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture):
    def callback(span: ReadableSpan) -> float:
        raise ValueError("boom")

    configure(write_token=TOKEN, sample_on_request=callback)
    tracer = create_tracer(exporter)
    with caplog.at_level(logging.WARNING, logger="apitally"):
        with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
            pass
    assert len(exporter.get_finished_spans()) == 1
    assert any("sample_on_request" in r.getMessage() for r in caplog.records)


def test_invalid_sample_callback_return_warns_and_keeps(
    exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
):
    configure(write_token=TOKEN, sample_rate=0.0, sample_on_request=lambda span: "yes")
    tracer = create_tracer(exporter)
    with caplog.at_level(logging.WARNING, logger="apitally"):
        with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
            pass
    assert len(exporter.get_finished_spans()) == 1
    assert any("sample_on_request" in r.getMessage() for r in caplog.records)


def test_sample_on_response_keeps_errors_drops_healthy(exporter: InMemorySpanExporter):
    bound = TraceIdRatioBased.get_bound_for_rate(0.05)
    configure(
        write_token=TOKEN,
        sample_on_response=lambda span: True if span.attributes.get("http.response.status_code") == 500 else 0.05,
    )
    tracer = create_tracer(exporter)
    with tracer.start_as_current_span("GET /a", kind=SpanKind.SERVER, context=remote_parent_context(bound)) as span:
        with tracer.start_as_current_span("child"):
            pass
        span.set_attribute("http.response.status_code", 200)
    # The healthy request is dropped at span end, after its child was already forwarded (spec section 6.5 orphan)
    assert [s.name for s in exporter.get_finished_spans()] == ["child"]

    exporter.clear()
    with tracer.start_as_current_span("GET /b", kind=SpanKind.SERVER, context=remote_parent_context(bound)) as span:
        span.set_attribute("http.response.status_code", 500)
    assert [s.name for s in exporter.get_finished_spans()] == ["GET /b"]


def test_same_trace_id_verdict_at_both_stages(exporter: InMemorySpanExporter):
    configure(write_token=TOKEN, sample_rate=0.5, sample_on_response=lambda span: 0.5)
    tracer = create_tracer(exporter)
    with tracer.start_as_current_span(
        "GET /items", kind=SpanKind.SERVER, context=remote_parent_context(BOUND_HALF - 1)
    ):
        pass
    # Both stages test the same trace ID against the same bound, so a kept request survives both
    assert len(exporter.get_finished_spans()) == 1


def test_response_abstention_leaves_boosted_request_kept(exporter: InMemorySpanExporter):
    bound_tenth = TraceIdRatioBased.get_bound_for_rate(0.1)
    configure(
        write_token=TOKEN,
        sample_rate=0.1,
        sample_on_request=lambda span: True,
        sample_on_response=lambda span: None,
    )
    tracer = create_tracer(exporter)
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER, context=remote_parent_context(bound_tenth)):
        pass
    # The trace ID fails the sample_rate test, so a response-stage abstention that re-tested it would drop here
    assert len(exporter.get_finished_spans()) == 1


def test_kept_flag_and_span_resolution(exporter: InMemorySpanExporter):
    configure(write_token=TOKEN, sample_rate=0.5)
    processor = ApitallySpanProcessor(SimpleSpanProcessor(exporter))
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(processor)
    tracer = provider.get_tracer(CONTRIB_SCOPE)

    with tracer.start_as_current_span(
        "GET /items", kind=SpanKind.SERVER, context=remote_parent_context(BOUND_HALF - 1)
    ) as kept_span:
        assert is_server_span_kept()
        assert get_server_span() is kept_span
        span_id = kept_span.get_span_context().span_id
        assert processor.resolve_server_span_id(span_id) == span_id

    with tracer.start_as_current_span(
        "GET /items", kind=SpanKind.SERVER, context=remote_parent_context(BOUND_HALF)
    ) as dropped_span:
        # The dropped request reads its own flag and span, not stale state from the kept request before it
        assert not is_server_span_kept()
        assert get_server_span() is dropped_span
        assert processor.resolve_server_span_id(dropped_span.get_span_context().span_id) is None


def test_contrib_send_receive_spans_dropped_user_spans_kept(
    tracer_provider: TracerProvider, tracer: Tracer, exporter: InMemorySpanExporter
):
    user_tracer = tracer_provider.get_tracer("myapp")
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        with tracer.start_as_current_span("GET /items http receive"):
            pass
        with tracer.start_as_current_span("GET /items http send"):
            pass
        with user_tracer.start_as_current_span("my http send"):
            pass
    assert {s.name for s in exporter.get_finished_spans()} == {"GET /items", "my http send"}


def test_forwarded_span_redacted_and_original_unmutated(tracer: Tracer, exporter: InMemorySpanExporter):
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


def test_client_span_url_full_redacted(tracer: Tracer, exporter: InMemorySpanExporter):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        with tracer.start_as_current_span(
            "GET", kind=SpanKind.CLIENT, attributes={"url.full": "https://x.example/v1?api-key=secret&ok=1"}
        ):
            pass
    client = next(s for s in exporter.get_finished_spans() if s.kind == SpanKind.CLIENT)
    url = str(unwrap(client.attributes)["url.full"])
    assert url.startswith("https://x.example/v1?")
    assert dict(parse_qsl(url.partition("?")[2])) == {"api-key": REDACTED, "ok": "1"}


def test_context_var_resolves_server_span_inside_request_only(tracer: Tracer):
    def handle_request() -> None:
        with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as server:
            with tracer.start_as_current_span("child"):
                assert get_server_span() is server

    assert get_server_span() is None
    contextvars.copy_context().run(handle_request)
    assert get_server_span() is None
