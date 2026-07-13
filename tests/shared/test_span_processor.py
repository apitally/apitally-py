import contextvars

import pytest
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, TraceIdRatioBased
from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, TraceFlags, Tracer

from apitally.shared.config import set_config
from apitally.shared.consumer import get_consumer_identifier, reset_consumer, set_consumer
from apitally.shared.span_processor import (
    MAX_BUFFERED_SPANS,
    ApitallySpanProcessor,
    get_server_span,
    is_server_span_kept,
)
from tests.conftest import CONTRIB_SCOPE, WRITE_TOKEN, create_tracer, unwrap


BOUND_HALF = TraceIdRatioBased.get_bound_for_rate(0.5)


def remote_parent_context(trace_id: int) -> Context:
    remote = SpanContext(trace_id=trace_id, span_id=1, is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED))
    return trace.set_span_in_context(NonRecordingSpan(remote))


@pytest.fixture()
def processor(span_exporter: InMemorySpanExporter) -> ApitallySpanProcessor:
    return ApitallySpanProcessor(SimpleSpanProcessor(span_exporter))


@pytest.fixture()
def tracer_provider(processor: ApitallySpanProcessor) -> TracerProvider:
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(processor)
    return provider


@pytest.fixture()
def tracer(tracer_provider: TracerProvider) -> Tracer:
    return tracer_provider.get_tracer(CONTRIB_SCOPE)


def test_server_root_and_child_kept(
    tracer: Tracer, processor: ApitallySpanProcessor, span_exporter: InMemorySpanExporter
):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as server:
        with tracer.start_as_current_span("child") as child:
            assert (
                processor.resolve_server_span_id(child.get_span_context().span_id) == server.get_span_context().span_id
            )
    assert {s.name for s in span_exporter.get_finished_spans()} == {"GET /items", "child"}
    assert not processor.spans
    assert not processor.pending


def test_nothing_exported_before_server_span_ends(tracer: Tracer, span_exporter: InMemorySpanExporter):
    # A request's telemetry is exported when the request completes, buffered descendants first
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        with tracer.start_as_current_span("child"):
            pass
        assert span_exporter.get_finished_spans() == ()
    assert [s.name for s in span_exporter.get_finished_spans()] == ["child", "GET /items"]


def test_server_span_with_unsampled_remote_parent_kept(tracer: Tracer, span_exporter: InMemorySpanExporter):
    remote = SpanContext(trace_id=1, span_id=2, is_remote=True, trace_flags=TraceFlags(TraceFlags.DEFAULT))
    context = trace.set_span_in_context(NonRecordingSpan(remote))
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER, context=context):
        pass
    assert len(span_exporter.get_finished_spans()) == 1


def test_non_server_root_and_children_dropped(tracer: Tracer, span_exporter: InMemorySpanExporter):
    with tracer.start_as_current_span("background job"):
        with tracer.start_as_current_span("child"):
            pass
    assert span_exporter.get_finished_spans() == ()


def test_options_request_dropped(tracer: Tracer, span_exporter: InMemorySpanExporter):
    attributes = {"http.request.method": "OPTIONS", "url.path": "/items"}
    with tracer.start_as_current_span("OPTIONS /items", kind=SpanKind.SERVER, attributes=attributes):
        pass
    assert span_exporter.get_finished_spans() == ()


@pytest.mark.parametrize(
    "attributes",
    [
        {"http.request.method": "GET", "url.path": "/healthz"},
        {"http.request.method": "GET", "url.path": "/", "user_agent.original": "kube-probe/1.30"},
        {"http.method": "GET", "http.target": "/healthz?full=1"},
        {"http.method": "GET", "http.target": "/", "http.user_agent": "kube-probe/1.30"},
    ],
)
def test_default_excluded_requests_dropped(
    tracer: Tracer, span_exporter: InMemorySpanExporter, attributes: dict[str, str]
):
    with tracer.start_as_current_span("GET", kind=SpanKind.SERVER, attributes=attributes):
        pass
    assert span_exporter.get_finished_spans() == ()


def test_user_exclude_paths_add_to_defaults(span_exporter: InMemorySpanExporter):
    set_config(write_token=WRITE_TOKEN, exclude_paths=[r"^/internal/"])
    tracer = create_tracer(span_exporter)
    for path in ["/internal/jobs", "/healthz", "/items"]:
        with tracer.start_as_current_span(f"GET {path}", kind=SpanKind.SERVER, attributes={"url.path": path}):
            pass
    assert [s.name for s in span_exporter.get_finished_spans()] == ["GET /items"]


def test_sample_rate_deterministic_by_trace_id(span_exporter: InMemorySpanExporter):
    set_config(write_token=WRITE_TOKEN, sample_rate=0.5)
    tracer = create_tracer(span_exporter)
    for trace_id in (BOUND_HALF - 1, BOUND_HALF):
        with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER, context=remote_parent_context(trace_id)):
            pass
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert unwrap(spans[0].context).trace_id == BOUND_HALF - 1


def test_sample_on_request_bool_overrides_sample_rate(span_exporter: InMemorySpanExporter):
    set_config(
        write_token=WRITE_TOKEN,
        sample_rate=0.0,
        sample_on_request=lambda span: span.attributes.get("url.path") != "/bots",
    )
    tracer = create_tracer(span_exporter)
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER, attributes={"url.path": "/items"}):
        pass
    with tracer.start_as_current_span("GET /bots", kind=SpanKind.SERVER, attributes={"url.path": "/bots"}):
        with tracer.start_as_current_span("child"):
            pass
    assert [s.name for s in span_exporter.get_finished_spans()] == ["GET /items"]


def test_sample_on_request_float_with_none_fallback(span_exporter: InMemorySpanExporter):
    bound_low = TraceIdRatioBased.get_bound_for_rate(0.01)
    set_config(
        write_token=WRITE_TOKEN,
        sample_rate=0.5,
        sample_on_request=lambda span: 0.01 if span.attributes.get("url.path") == "/noisy" else None,
    )
    tracer = create_tracer(span_exporter)
    for path, trace_id, kept in [
        ("/noisy", bound_low - 1, True),
        ("/noisy", bound_low, False),
        ("/other", BOUND_HALF - 1, True),
        ("/other", BOUND_HALF, False),
    ]:
        span_exporter.clear()
        with tracer.start_as_current_span(
            f"GET {path}", kind=SpanKind.SERVER, context=remote_parent_context(trace_id), attributes={"url.path": path}
        ):
            pass
        assert bool(span_exporter.get_finished_spans()) is kept, (path, trace_id)


def test_excluded_request_never_invokes_sample_callback(span_exporter: InMemorySpanExporter):
    calls: list[ReadableSpan] = []

    def callback(span: ReadableSpan) -> None:
        calls.append(span)

    set_config(write_token=WRITE_TOKEN, sample_on_request=callback)
    tracer = create_tracer(span_exporter)
    with tracer.start_as_current_span("GET /healthz", kind=SpanKind.SERVER, attributes={"url.path": "/healthz"}):
        pass
    assert not calls
    assert span_exporter.get_finished_spans() == ()


def test_raising_sample_on_request_keeps_span(span_exporter: InMemorySpanExporter):
    def callback(span: ReadableSpan) -> float:
        raise ValueError("boom")

    set_config(write_token=WRITE_TOKEN, sample_on_request=callback)
    tracer = create_tracer(span_exporter)
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        pass
    assert len(span_exporter.get_finished_spans()) == 1


def test_invalid_sample_on_request_return_keeps_span(span_exporter: InMemorySpanExporter):
    set_config(write_token=WRITE_TOKEN, sample_rate=0.0, sample_on_request=lambda span: "yes")
    tracer = create_tracer(span_exporter)
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        pass
    assert len(span_exporter.get_finished_spans()) == 1


def test_sample_on_response_keeps_errors_drops_healthy(span_exporter: InMemorySpanExporter):
    bound = TraceIdRatioBased.get_bound_for_rate(0.05)
    set_config(
        write_token=WRITE_TOKEN,
        sample_on_response=lambda span: True if span.attributes.get("http.response.status_code") == 500 else 0.05,
    )
    tracer = create_tracer(span_exporter)
    with tracer.start_as_current_span("GET /a", kind=SpanKind.SERVER, context=remote_parent_context(bound)) as span:
        with tracer.start_as_current_span("child"):
            pass
        span.set_attribute("http.response.status_code", 200)
    # The buffered child is discarded with its dropped request: zero items exported
    assert span_exporter.get_finished_spans() == ()

    with tracer.start_as_current_span("GET /b", kind=SpanKind.SERVER, context=remote_parent_context(bound)) as span:
        with tracer.start_as_current_span("child"):
            pass
        span.set_attribute("http.response.status_code", 500)
    assert [s.name for s in span_exporter.get_finished_spans()] == ["child", "GET /b"]


def test_sampled_out_response_releases_stash(span_exporter: InMemorySpanExporter):
    set_config(write_token=WRITE_TOKEN, sample_on_response=lambda span: False)
    processor = ApitallySpanProcessor(SimpleSpanProcessor(span_exporter))
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(processor)
    tracer = provider.get_tracer(CONTRIB_SCOPE)

    with tracer.start_as_current_span("POST /items", kind=SpanKind.SERVER) as span:
        processor.update_stash(span.get_span_context().span_id, request_body=b'{"a": 1}')
    assert span_exporter.get_finished_spans() == ()
    assert processor.stash == {}


def test_span_buffer_cap_bounds_kept_and_dropped_requests(span_exporter: InMemorySpanExporter):
    set_config(
        write_token=WRITE_TOKEN,
        sample_on_response=lambda span: span.attributes.get("http.response.status_code") == 500,
    )
    tracer = create_tracer(span_exporter)
    for status_code, expected_count in [(500, MAX_BUFFERED_SPANS + 1), (200, 0)]:
        with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as span:
            for _ in range(MAX_BUFFERED_SPANS + 1):
                with tracer.start_as_current_span("child"):
                    pass
            span.set_attribute("http.response.status_code", status_code)
        assert len(span_exporter.get_finished_spans()) == expected_count
        span_exporter.clear()


def test_late_descendant_follows_request_decision(span_exporter: InMemorySpanExporter):
    set_config(
        write_token=WRITE_TOKEN,
        sample_on_response=lambda span: span.attributes.get("http.response.status_code") == 500,
    )
    tracer = create_tracer(span_exporter)
    for status_code, expected_names in [(500, {"GET /items", "late"}), (200, set())]:
        with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as span:
            late = tracer.start_span("late")
            span.set_attribute("http.response.status_code", status_code)
        # A span ending after a kept request is exported immediately; after a dropped request it is discarded
        late.end()
        assert {s.name for s in span_exporter.get_finished_spans()} == expected_names
        span_exporter.clear()


def test_deferred_export_held_until_finish(
    tracer: Tracer, processor: ApitallySpanProcessor, span_exporter: InMemorySpanExporter
):
    with tracer.start_as_current_span("GET /stream", kind=SpanKind.SERVER) as server:
        with tracer.start_as_current_span("child"):
            pass
        span_id = server.get_span_context().span_id
        processor.defer_export(span_id)
    # The span has ended but the transport has not completed the response yet
    assert span_exporter.get_finished_spans() == ()
    processor.finish_export(span_id, {"http.response.body.size": 45})
    spans = {s.name: s for s in span_exporter.get_finished_spans()}
    assert set(spans) == {"GET /stream", "child"}
    assert unwrap(spans["GET /stream"].attributes)["http.response.body.size"] == 45
    assert not processor.spans and not processor.pending and not processor.deferred and not processor.held


def test_finish_export_before_span_end_writes_to_live_span(
    tracer: Tracer, processor: ApitallySpanProcessor, span_exporter: InMemorySpanExporter
):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as server:
        span_id = server.get_span_context().span_id
        processor.defer_export(span_id)
        processor.finish_export(span_id, {"http.response.body.size": 10})
    (span,) = span_exporter.get_finished_spans()
    assert unwrap(span.attributes)["http.response.body.size"] == 10
    assert not processor.deferred and not processor.held


def test_finish_export_without_attributes_releases_span(
    tracer: Tracer, processor: ApitallySpanProcessor, span_exporter: InMemorySpanExporter
):
    with tracer.start_as_current_span("GET /stream", kind=SpanKind.SERVER) as server:
        span_id = server.get_span_context().span_id
        processor.defer_export(span_id)
    processor.finish_export(span_id)
    (span,) = span_exporter.get_finished_spans()
    assert "http.response.body.size" not in unwrap(span.attributes)


def test_shutdown_exports_held_spans(
    tracer: Tracer, processor: ApitallySpanProcessor, span_exporter: InMemorySpanExporter
):
    with tracer.start_as_current_span("GET /stream", kind=SpanKind.SERVER) as server:
        processor.defer_export(server.get_span_context().span_id)
    assert span_exporter.get_finished_spans() == ()
    processor.shutdown()
    (span,) = span_exporter.get_finished_spans()
    assert span.name == "GET /stream"


def test_shutdown_flushes_queued_spans(span_exporter: InMemorySpanExporter):
    processor = ApitallySpanProcessor(BatchSpanProcessor(span_exporter))
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(processor)
    with provider.get_tracer(CONTRIB_SCOPE).start_as_current_span("GET /items", kind=SpanKind.SERVER):
        pass
    assert span_exporter.get_finished_spans() == ()  # released to the batch processor, still queued
    provider.shutdown()
    (span,) = span_exporter.get_finished_spans()
    assert span.name == "GET /items"


def test_sampling_decision_consistent_between_request_and_response(span_exporter: InMemorySpanExporter):
    set_config(write_token=WRITE_TOKEN, sample_rate=0.5, sample_on_response=lambda span: 0.5)
    tracer = create_tracer(span_exporter)
    with tracer.start_as_current_span(
        "GET /items", kind=SpanKind.SERVER, context=remote_parent_context(BOUND_HALF - 1)
    ):
        pass
    # Both stages test the same trace ID against the same bound, so a kept request survives both
    assert len(span_exporter.get_finished_spans()) == 1


def test_sample_on_response_none_keeps_sample_on_request_decision(span_exporter: InMemorySpanExporter):
    bound_tenth = TraceIdRatioBased.get_bound_for_rate(0.1)
    set_config(
        write_token=WRITE_TOKEN,
        sample_rate=0.1,
        sample_on_request=lambda span: True,
        sample_on_response=lambda span: None,
    )
    tracer = create_tracer(span_exporter)
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER, context=remote_parent_context(bound_tenth)):
        pass
    # The trace ID fails the sample_rate test, so if the None return re-tested it, the span would be dropped here
    assert len(span_exporter.get_finished_spans()) == 1


def test_request_context_helpers_return_current_request_state(span_exporter: InMemorySpanExporter):
    set_config(write_token=WRITE_TOKEN, sample_rate=0.5)
    processor = ApitallySpanProcessor(SimpleSpanProcessor(span_exporter))
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(processor)
    tracer = provider.get_tracer(CONTRIB_SCOPE)

    with tracer.start_as_current_span(
        "GET /items", kind=SpanKind.SERVER, context=remote_parent_context(BOUND_HALF - 1)
    ) as kept_span:
        assert is_server_span_kept()
        assert get_server_span() is kept_span
        set_consumer("tenant-1")
        span_id = kept_span.get_span_context().span_id
        assert processor.resolve_server_span_id(span_id) == span_id

    reset_consumer()  # the transport middleware does this at request entry
    with tracer.start_as_current_span(
        "GET /items", kind=SpanKind.SERVER, context=remote_parent_context(BOUND_HALF)
    ) as dropped_span:
        # The dropped request reads its own flag, span, and consumer, not stale state from the kept request
        assert not is_server_span_kept()
        assert get_server_span() is dropped_span
        assert processor.resolve_server_span_id(dropped_span.get_span_context().span_id) is None
        assert get_consumer_identifier() is None


def test_contrib_send_receive_spans_dropped_user_spans_kept(
    tracer_provider: TracerProvider, tracer: Tracer, span_exporter: InMemorySpanExporter
):
    user_tracer = tracer_provider.get_tracer("myapp")
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        with tracer.start_as_current_span("GET /items http receive"):
            pass
        with tracer.start_as_current_span("GET /items http send"):
            pass
        with user_tracer.start_as_current_span("my http send"):
            pass
    assert {s.name for s in span_exporter.get_finished_spans()} == {"GET /items", "my http send"}


def test_context_var_resolves_server_span_inside_request_only(tracer: Tracer):
    def handle_request() -> None:
        with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as server:
            with tracer.start_as_current_span("child"):
                assert get_server_span() is server

    assert get_server_span() is None
    contextvars.copy_context().run(handle_request)
    assert get_server_span() is None
