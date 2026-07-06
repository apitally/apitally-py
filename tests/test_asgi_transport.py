from __future__ import annotations

import contextvars
import json
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import ExponentialHistogram, InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
from opentelemetry.trace import SpanKind, Tracer

from apitally.shared import config, metrics
from apitally.shared.asgi import ApitallyASGIMiddleware
from apitally.shared.capture import BODY_TOO_LARGE
from apitally.shared.config import configure
from apitally.shared.consumer import set_consumer
from apitally.shared.redaction import REDACTED
from tests.conftest import WRITE_TOKEN, attach_metric_reader, collect_metrics, create_trace_pipeline


JSON_HEADERS = [("content-type", "application/json")]


@pytest.fixture(autouse=True)
def reset_config() -> Iterator[None]:
    yield
    config.reset()


@pytest.fixture(autouse=True)
def metric_reader() -> Iterator[InMemoryMetricReader]:
    reader = attach_metric_reader(metrics.setup(Resource.create({})))
    yield reader
    metrics.reset()


def header_values(span: ReadableSpan, key: str) -> tuple[str, ...]:
    value = (span.attributes or {})[key]
    assert isinstance(value, (list, tuple))
    return tuple(str(v) for v in value)


class EchoApp:
    def __init__(
        self,
        status: int = 200,
        response_headers: list[tuple[str, str]] | None = None,
        response_chunks: list[bytes] | None = None,
        on_request: Any = None,
    ) -> None:
        self.status = status
        self.response_headers = [(k.encode(), v.encode()) for k, v in response_headers or []]
        self.response_chunks = response_chunks or [b"ok"]
        self.on_request = on_request
        self.received_messages: list[dict[str, Any]] = []
        self.received_receive: Any = None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.received_receive = receive
        while True:
            message = await receive()
            self.received_messages.append(message)
            if not message.get("more_body", False):
                break
        if self.on_request is not None:
            self.on_request()
        await send({"type": "http.response.start", "status": self.status, "headers": self.response_headers})
        for i, chunk in enumerate(self.response_chunks):
            await send({"type": "http.response.body", "body": chunk, "more_body": i + 1 < len(self.response_chunks)})


def make_scope(
    method: str = "POST", route: str = "/items", headers: list[tuple[str, str]] | None = None
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "scheme": "http",
        "path": "/items",
        "route": route,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers or []],
    }


async def send_request(
    tracer: Tracer,
    app: EchoApp,
    request_headers: list[tuple[str, str]] | None = None,
    request_chunks: list[bytes] | None = None,
    method: str = "POST",
    route: str = "/items",
) -> list[dict[str, Any]]:
    middleware = ApitallyASGIMiddleware(app)
    scope = make_scope(method=method, route=route, headers=request_headers)
    chunks = request_chunks or [b""]
    messages = [
        {"type": "http.request", "body": chunk, "more_body": i + 1 < len(chunks)} for i, chunk in enumerate(chunks)
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    with tracer.start_as_current_span(f"{method} {route}", kind=SpanKind.SERVER):
        await middleware(scope, receive, send)
    return sent


async def test_json_bodies_captured_and_redacted():
    configure(write_token=WRITE_TOKEN, log_request_body=True, log_response_body=True)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp(response_headers=JSON_HEADERS, response_chunks=[b'{"token": "t", "ok": true}'])
    await send_request(tracer, app, request_headers=JSON_HEADERS, request_chunks=[b'{"password": "x", "user": "u"}'])

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert json.loads(str(span.attributes["apitally.request.body"])) == {"password": REDACTED, "user": "u"}
    assert json.loads(str(span.attributes["apitally.response.body"])) == {"token": REDACTED, "ok": True}


async def test_capture_off_passthrough_and_size_from_content_length():
    configure(write_token=WRITE_TOKEN)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp()
    middleware = ApitallyASGIMiddleware(app)
    scope = make_scope(headers=JSON_HEADERS + [("content-length", "17")])

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b'{"password": "x"}', "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        pass

    with tracer.start_as_current_span("POST /items", kind=SpanKind.SERVER):
        await middleware(scope, receive, send)

    assert app.received_receive is receive  # no wrapping, zero buffering
    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert "apitally.request.body" not in span.attributes
    assert "apitally.response.body" not in span.attributes
    # Size attributes are independent of the capture toggles (R10)
    assert span.attributes["http.request.body.size"] == 17


@pytest.mark.parametrize(
    ("content_type", "captured"),
    [("image/png", False), ("text/plain; charset=utf-8", True)],
)
async def test_content_type_allowlist(content_type: str, captured: bool):
    configure(write_token=WRITE_TOKEN, log_request_body=True)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp()
    await send_request(tracer, app, request_headers=[("content-type", content_type)], request_chunks=[b"hello"])

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert ("apitally.request.body" in span.attributes) is captured
    if captured:
        assert span.attributes["apitally.request.body"] == "hello"
    else:
        assert app.received_receive is not None  # app still received the body untouched
        assert app.received_messages[0]["body"] == b"hello"


async def test_body_over_cap_sentinel_with_passthrough():
    configure(write_token=WRITE_TOKEN, log_request_body=True)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp()
    chunks = [b"a" * 30_000, b"b" * 30_000]
    await send_request(tracer, app, request_headers=JSON_HEADERS, request_chunks=chunks)

    assert [m["body"] for m in app.received_messages] == chunks  # byte-identical downstream
    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes["apitally.request.body"] == BODY_TOO_LARGE
    assert span.attributes["http.request.body.size"] == 60_000


async def test_mask_callback_none_or_raise_yields_masked():
    def mask(span: ReadableSpan, body: bytes) -> bytes | None:
        if b"boom" in body:
            raise ValueError("boom")
        return None

    configure(write_token=WRITE_TOKEN, log_request_body=True, mask_request_body=mask)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp()
    await send_request(tracer, app, request_headers=JSON_HEADERS, request_chunks=[b'{"a": 1}'])
    await send_request(tracer, app, request_headers=JSON_HEADERS, request_chunks=[b'{"a": "boom"}'])

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    for span in spans:
        assert span.attributes is not None
        assert span.attributes["apitally.request.body"] == REDACTED


async def test_mask_callback_output_over_cap_yields_too_large():
    configure(write_token=WRITE_TOKEN, log_request_body=True, mask_request_body=lambda span, body: b"x" * 50_001)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp()
    await send_request(tracer, app, request_headers=JSON_HEADERS, request_chunks=[b'{"a": 1}'])

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes["apitally.request.body"] == BODY_TOO_LARGE


async def test_aborted_response_body_not_exported():
    configure(write_token=WRITE_TOKEN, log_response_body=True)
    tracer, exporter = create_trace_pipeline()

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"a":', "more_body": True})
        raise RuntimeError("aborted mid-stream")

    middleware = ApitallyASGIMiddleware(app)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        pass

    with tracer.start_as_current_span("POST /items", kind=SpanKind.SERVER):
        with pytest.raises(RuntimeError):
            await middleware(make_scope(), receive, send)

    (span,) = exporter.get_finished_spans()
    assert "apitally.response.body" not in (span.attributes or {})


async def test_invalid_user_pattern_dropped_and_request_succeeds():
    configure(write_token=WRITE_TOKEN, log_request_headers=True, mask_headers=["("])
    tracer, exporter = create_trace_pipeline()
    app = EchoApp()
    await send_request(tracer, app, request_headers=[("Authorization", "Bearer x")])

    (span,) = exporter.get_finished_spans()
    assert header_values(span, "http.request.header.authorization") == (REDACTED,)


async def test_headers_redacted_and_repeated_as_list():
    configure(write_token=WRITE_TOKEN, log_request_headers=True, log_response_headers=True)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp(response_headers=[("x-item", "a"), ("x-item", "b"), ("x-secret-key", "s")])
    await send_request(tracer, app, request_headers=[("Authorization", "Bearer x"), ("user-agent", "test")])

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert header_values(span, "http.request.header.authorization") == (REDACTED,)
    assert header_values(span, "http.request.header.user-agent") == ("test",)
    assert header_values(span, "http.response.header.x-item") == ("a", "b")
    assert header_values(span, "http.response.header.x-secret-key") == (REDACTED,)


async def test_size_backfill_and_chunked_response_counter():
    configure(write_token=WRITE_TOKEN, log_request_body=True)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp(response_chunks=[b"aa", b"bbb"])  # no Content-Length
    await send_request(tracer, app, request_headers=JSON_HEADERS, request_chunks=[b'{"a"', b": 1}"])

    # Presence on the ended span proves the attributes were written while it was still recording
    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes["http.request.body.size"] == 8
    assert span.attributes["http.response.body.size"] == 5


async def test_histogram_records_once_with_consumer(metric_reader: InMemoryMetricReader):
    configure(write_token=WRITE_TOKEN)
    tracer, _ = create_trace_pipeline()
    app = EchoApp(on_request=lambda: set_consumer("tenant-1"))
    await send_request(tracer, app, method="GET", route="/items/{id}")

    duration_metric = collect_metrics(metric_reader)["http.server.request.duration"]
    assert isinstance(duration_metric.data, ExponentialHistogram)
    (point,) = duration_metric.data.data_points
    assert point.count == 1
    assert dict(point.attributes or {}) == {
        "http.request.method": "GET",
        "http.route": "/items/{id}",
        "http.response.status_code": 200,
        "apitally.consumer.identifier": "tenant-1",
        "url.scheme": "http",
    }


async def test_sampled_out_request_still_records_metrics(metric_reader: InMemoryMetricReader):
    configure(write_token=WRITE_TOKEN)
    tracer, exporter = create_trace_pipeline(sampler=TraceIdRatioBased(0.0))
    app = EchoApp(on_request=lambda: set_consumer("tenant-1"))
    await send_request(tracer, app, method="GET")

    assert exporter.get_finished_spans() == ()
    duration_metric = collect_metrics(metric_reader)["http.server.request.duration"]
    assert isinstance(duration_metric.data, ExponentialHistogram)
    (point,) = duration_metric.data.data_points
    assert (point.attributes or {})["apitally.consumer.identifier"] == "tenant-1"


async def test_apitally_sampled_out_request_still_records_metrics(metric_reader: InMemoryMetricReader):
    # Apitally-sampling twin of the cooperative-sampler test above (R9, R12)
    configure(write_token=WRITE_TOKEN, sample_rate=0.0)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp(on_request=lambda: set_consumer("tenant-1"))
    await send_request(tracer, app, method="GET")

    assert exporter.get_finished_spans() == ()
    duration_metric = collect_metrics(metric_reader)["http.server.request.duration"]
    assert isinstance(duration_metric.data, ExponentialHistogram)
    (point,) = duration_metric.data.data_points
    assert (point.attributes or {})["apitally.consumer.identifier"] == "tenant-1"


async def test_response_stage_dropped_request_still_records_metrics(metric_reader: InMemoryMetricReader):
    # Response-stage twin: buffered spans discard at SERVER end while metrics record regardless
    configure(write_token=WRITE_TOKEN, sample_on_response=lambda span: False)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp(on_request=lambda: set_consumer("tenant-1"))
    await send_request(tracer, app, method="GET")

    assert exporter.get_finished_spans() == ()
    duration_metric = collect_metrics(metric_reader)["http.server.request.duration"]
    assert isinstance(duration_metric.data, ExponentialHistogram)
    (point,) = duration_metric.data.data_points
    assert (point.attributes or {})["apitally.consumer.identifier"] == "tenant-1"


async def test_sampled_out_request_skips_capture(metric_reader: InMemoryMetricReader):
    mask_calls: list[bytes] = []

    def mask(span: ReadableSpan, body: bytes) -> bytes:
        mask_calls.append(body)
        return body

    configure(write_token=WRITE_TOKEN, sample_rate=0.0, log_request_body=True, mask_request_body=mask)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp()
    middleware = ApitallyASGIMiddleware(app)
    scope = make_scope(headers=JSON_HEADERS)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b'{"a": 1}', "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        pass

    with tracer.start_as_current_span("POST /items", kind=SpanKind.SERVER):
        await middleware(scope, receive, send)

    assert app.received_receive is receive  # no wrapping, zero buffering
    assert not mask_calls
    assert exporter.get_finished_spans() == ()
    duration_metric = collect_metrics(metric_reader)["http.server.request.duration"]
    assert isinstance(duration_metric.data, ExponentialHistogram)
    (point,) = duration_metric.data.data_points
    assert point.count == 1


async def test_sampled_out_sync_endpoint_consumer_reaches_metrics(metric_reader: InMemoryMetricReader):
    # Sync endpoints run in a copied context where set_consumer's ContextVar write is lost;
    # the span-attribute fallback must carry the consumer even for a sampled-out request (R11)
    configure(write_token=WRITE_TOKEN, sample_rate=0.0)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp(on_request=lambda: contextvars.copy_context().run(set_consumer, "tenant-1"))
    await send_request(tracer, app, method="GET")

    assert exporter.get_finished_spans() == ()
    duration_metric = collect_metrics(metric_reader)["http.server.request.duration"]
    assert isinstance(duration_metric.data, ExponentialHistogram)
    (point,) = duration_metric.data.data_points
    assert (point.attributes or {})["apitally.consumer.identifier"] == "tenant-1"
