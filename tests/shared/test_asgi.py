import contextvars
import json
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import ExponentialHistogram, InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Sampler, TraceIdRatioBased
from opentelemetry.trace import SpanKind, Tracer

from apitally.shared import config, metrics
from apitally.shared.asgi import ApitallyASGIMiddleware
from apitally.shared.capture import BODY_TOO_LARGE
from apitally.shared.config import set_config
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
    set_config(write_token=WRITE_TOKEN, log_request_body=True, log_response_body=True)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp(response_headers=JSON_HEADERS, response_chunks=[b'{"token": "t", "ok": true}'])
    await send_request(tracer, app, request_headers=JSON_HEADERS, request_chunks=[b'{"password": "x", "user": "u"}'])

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert json.loads(str(span.attributes["apitally.request.body"])) == {"password": REDACTED, "user": "u"}
    assert json.loads(str(span.attributes["apitally.response.body"])) == {"token": REDACTED, "ok": True}


async def test_capture_off_passthrough_and_size_from_content_length():
    set_config(write_token=WRITE_TOKEN)
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
    # Size attributes are independent of the capture toggles
    assert span.attributes["http.request.body.size"] == 17


@pytest.mark.parametrize(
    ("content_type", "captured"),
    [("image/png", False), ("text/plain; charset=utf-8", True)],
)
async def test_content_type_allowlist(content_type: str, captured: bool):
    set_config(write_token=WRITE_TOKEN, log_request_body=True)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp()
    await send_request(tracer, app, request_headers=[("content-type", content_type)], request_chunks=[b"hello"])

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert ("apitally.request.body" in span.attributes) is captured
    if captured:
        assert span.attributes["apitally.request.body"] == "hello"
    else:
        assert app.received_messages[0]["body"] == b"hello"  # app still received the body untouched


async def test_body_over_cap_sentinel_with_passthrough():
    set_config(write_token=WRITE_TOKEN, log_request_body=True)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp()
    chunks = [b"a" * 30_000, b"b" * 30_000]
    await send_request(tracer, app, request_headers=JSON_HEADERS, request_chunks=chunks)

    assert [m["body"] for m in app.received_messages] == chunks  # byte-identical downstream
    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes["apitally.request.body"] == BODY_TOO_LARGE
    assert span.attributes["http.request.body.size"] == 60_000


@pytest.mark.parametrize("extra_headers", [[("content-length", "60000")], []], ids=["declared", "mid-stream"])
async def test_response_body_over_cap_sentinel(extra_headers: list[tuple[str, str]]):
    set_config(write_token=WRITE_TOKEN, log_response_body=True)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp(response_headers=JSON_HEADERS + extra_headers, response_chunks=[b"a" * 30_000, b"b" * 30_000])
    await send_request(tracer, app)

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes["apitally.response.body"] == BODY_TOO_LARGE
    assert span.attributes["http.response.body.size"] == 60_000


async def test_mask_request_body_result_exported_after_redaction():
    def mask(span: ReadableSpan, body: bytes) -> bytes:
        return b'{"password": "x", "card": "masked"}'

    set_config(write_token=WRITE_TOKEN, log_request_body=True, mask_request_body=mask)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp()
    await send_request(tracer, app, request_headers=JSON_HEADERS, request_chunks=[b'{"card": "4111111111111111"}'])

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert json.loads(str(span.attributes["apitally.request.body"])) == {"password": REDACTED, "card": "masked"}


async def test_mask_request_body_none_or_raise_yields_redacted():
    def mask(span: ReadableSpan, body: bytes) -> bytes | None:
        if b"boom" in body:
            raise ValueError("boom")
        return None

    set_config(write_token=WRITE_TOKEN, log_request_body=True, mask_request_body=mask)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp()
    await send_request(tracer, app, request_headers=JSON_HEADERS, request_chunks=[b'{"a": 1}'])
    await send_request(tracer, app, request_headers=JSON_HEADERS, request_chunks=[b'{"a": "boom"}'])

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    for span in spans:
        assert span.attributes is not None
        assert span.attributes["apitally.request.body"] == REDACTED


async def test_mask_request_body_result_over_cap_yields_too_large():
    set_config(write_token=WRITE_TOKEN, log_request_body=True, mask_request_body=lambda span, body: b"x" * 50_001)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp()
    await send_request(tracer, app, request_headers=JSON_HEADERS, request_chunks=[b'{"a": 1}'])

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes["apitally.request.body"] == BODY_TOO_LARGE


async def test_aborted_response_exports_headers_and_size_but_not_body():
    set_config(write_token=WRITE_TOKEN, log_request_headers=True, log_response_headers=True, log_response_body=True)
    tracer, exporter = create_trace_pipeline()

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        # The span starts and ends inside the app, mirroring the instrumentor ending the SERVER span
        # before the exception reaches the middleware's finally
        with tracer.start_as_current_span("POST /items", kind=SpanKind.SERVER):
            await receive()
            await send(
                {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]}
            )
            await send({"type": "http.response.body", "body": b'{"a":', "more_body": True})
            raise RuntimeError("aborted mid-stream")

    middleware = ApitallyASGIMiddleware(app)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        pass

    with pytest.raises(RuntimeError):
        await middleware(make_scope(headers=[("User-Agent", "test")]), receive, send)

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes["http.response.body.size"] == 5
    assert header_values(span, "http.request.header.user-agent") == ("test",)
    assert header_values(span, "http.response.header.content-type") == ("application/json",)
    assert "apitally.response.body" not in span.attributes


async def test_invalid_user_pattern_dropped_and_request_succeeds():
    set_config(write_token=WRITE_TOKEN, log_request_headers=True, mask_headers=["(", "x-custom"])
    tracer, exporter = create_trace_pipeline()
    app = EchoApp()
    await send_request(tracer, app, request_headers=[("Authorization", "Bearer x"), ("X-Custom", "v")])

    (span,) = exporter.get_finished_spans()
    assert header_values(span, "http.request.header.authorization") == (REDACTED,)
    # Only the invalid pattern is dropped; the valid one from the same list still applies
    assert header_values(span, "http.request.header.x-custom") == (REDACTED,)


async def test_headers_redacted_and_repeated_as_list():
    set_config(write_token=WRITE_TOKEN, log_request_headers=True, log_response_headers=True)
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
    set_config(write_token=WRITE_TOKEN, log_request_body=True)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp(response_chunks=[b"aa", b"bbb"])  # no Content-Length
    await send_request(tracer, app, request_headers=JSON_HEADERS, request_chunks=[b'{"a"', b": 1}"])

    # Presence on the ended span proves the attributes were written while it was still recording
    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes["http.request.body.size"] == 8
    assert span.attributes["http.response.body.size"] == 5


async def test_histogram_records_once_with_consumer(metric_reader: InMemoryMetricReader):
    set_config(write_token=WRITE_TOKEN)
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


@pytest.mark.parametrize(
    ("config_kwargs", "sampler"),
    [
        pytest.param({}, TraceIdRatioBased(0.0), id="user-sampler"),
        pytest.param({"sample_rate": 0.0}, ALWAYS_ON, id="apitally-sample-rate"),
        pytest.param({"sample_on_response": lambda span: False}, ALWAYS_ON, id="response-stage-drop"),
    ],
)
async def test_dropped_request_still_records_metrics(
    metric_reader: InMemoryMetricReader, config_kwargs: dict[str, Any], sampler: Sampler
):
    set_config(write_token=WRITE_TOKEN, **config_kwargs)
    tracer, exporter = create_trace_pipeline(sampler=sampler)
    app = EchoApp(on_request=lambda: set_consumer("tenant-1"))
    await send_request(tracer, app, method="GET")

    assert exporter.get_finished_spans() == ()
    duration_metric = collect_metrics(metric_reader)["http.server.request.duration"]
    assert isinstance(duration_metric.data, ExponentialHistogram)
    (point,) = duration_metric.data.data_points
    assert (point.attributes or {})["apitally.consumer.identifier"] == "tenant-1"


async def test_consumer_set_in_copied_context_still_reaches_metrics(metric_reader: InMemoryMetricReader):
    set_config(write_token=WRITE_TOKEN, sample_rate=0.0)
    tracer, exporter = create_trace_pipeline()

    def set_consumer_in_copied_context() -> None:
        # Mirrors sync endpoints: the copied context discards set_consumer's ContextVar write
        contextvars.copy_context().run(set_consumer, "tenant-1")

    app = EchoApp(on_request=set_consumer_in_copied_context)
    await send_request(tracer, app, method="GET")

    assert exporter.get_finished_spans() == ()
    duration_metric = collect_metrics(metric_reader)["http.server.request.duration"]
    assert isinstance(duration_metric.data, ExponentialHistogram)
    (point,) = duration_metric.data.data_points
    assert (point.attributes or {})["apitally.consumer.identifier"] == "tenant-1"


async def test_consumer_set_in_outer_middleware_reaches_span_and_metrics(metric_reader: InMemoryMetricReader):
    set_config(write_token=WRITE_TOKEN)
    tracer, exporter = create_trace_pipeline()
    app = EchoApp()

    async def instrumented_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        # SERVER span starts inside the transport middleware, as with instrumentors that wrap the app
        with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
            await app(scope, receive, send)

    middleware = ApitallyASGIMiddleware(instrumented_app)

    async def run_request_with_outer_consumer(identifier: str) -> None:
        set_consumer(identifier, name="Tenant", group="tenants")  # before the transport middleware runs
        messages = [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive() -> dict[str, Any]:
            return messages.pop(0)

        async def send(message: dict[str, Any]) -> None:
            pass

        await middleware(make_scope(method="GET"), receive, send)

    # The second request pins that a completed request does not swallow the next request's consumer
    await run_request_with_outer_consumer("tenant-1")
    await run_request_with_outer_consumer("tenant-2")

    spans = exporter.get_finished_spans()
    assert [(span.attributes or {})["apitally.consumer.identifier"] for span in spans] == ["tenant-1", "tenant-2"]
    assert (spans[0].attributes or {})["apitally.consumer.name"] == "Tenant"
    assert (spans[0].attributes or {})["apitally.consumer.group"] == "tenants"
    duration_metric = collect_metrics(metric_reader)["http.server.request.duration"]
    assert isinstance(duration_metric.data, ExponentialHistogram)
    consumers = {(p.attributes or {}).get("apitally.consumer.identifier") for p in duration_metric.data.data_points}
    assert consumers == {"tenant-1", "tenant-2"}


async def test_consumer_not_carried_over_to_next_request_in_same_context(metric_reader: InMemoryMetricReader):
    set_config(write_token=WRITE_TOKEN)
    tracer, _ = create_trace_pipeline()
    await send_request(tracer, EchoApp(on_request=lambda: set_consumer("tenant-1")), method="GET")
    await send_request(tracer, EchoApp(), method="GET")

    duration_metric = collect_metrics(metric_reader)["http.server.request.duration"]
    assert isinstance(duration_metric.data, ExponentialHistogram)
    consumers = {(p.attributes or {}).get("apitally.consumer.identifier") for p in duration_metric.data.data_points}
    assert consumers == {"tenant-1", None}


async def test_sampled_out_request_skips_capture(metric_reader: InMemoryMetricReader):
    mask_calls: list[bytes] = []

    def mask(span: ReadableSpan, body: bytes) -> bytes:
        mask_calls.append(body)
        return body

    set_config(write_token=WRITE_TOKEN, sample_rate=0.0, log_request_body=True, mask_request_body=mask)
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

    assert not mask_calls
    assert exporter.get_finished_spans() == ()
    duration_metric = collect_metrics(metric_reader)["http.server.request.duration"]
    assert isinstance(duration_metric.data, ExponentialHistogram)
    (point,) = duration_metric.data.data_points
    assert point.count == 1
