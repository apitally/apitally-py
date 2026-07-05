import io
import json
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import SpanKind

from apitally.shared.capture import BODY_TOO_LARGE
from apitally.shared.config import configure
from apitally.shared.redaction import REDACTED
from apitally.shared.span_processor import ApitallySpanProcessor, server_span_var
from apitally.shared.wsgi import ApitallyWSGIMiddleware


TOKEN = "apt_" + "a" * 24


class SpyInput:
    def __init__(self, data: bytes) -> None:
        self.stream = io.BytesIO(data)
        self.read_count = 0

    def read(self, size: int = -1) -> bytes:
        self.read_count += 1
        return self.stream.read(size)


class ClosingIterable:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        return iter(self.chunks)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def reset_server_span_var():
    yield
    server_span_var.set(None)


@pytest.fixture()
def exporter():
    return InMemorySpanExporter()


@pytest.fixture()
def tracer(exporter):
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(ApitallySpanProcessor(SimpleSpanProcessor(exporter)))
    return provider.get_tracer("test")


def make_environ(
    method: str = "GET",
    path: str = "/items",
    body: bytes = b"",
    content_type: str | None = None,
    content_length: str | None = None,
    **extra: str,
) -> dict[str, Any]:
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.url_scheme": "http",
        "wsgi.input": SpyInput(body),
    }
    if content_type is not None:
        environ["CONTENT_TYPE"] = content_type
    if content_length is not None:
        environ["CONTENT_LENGTH"] = content_length
    environ.update(extra)
    return environ


def run_request(
    middleware: ApitallyWSGIMiddleware,
    environ: dict[str, Any],
    tracer,
    exporter: InMemorySpanExporter,
    consume_chunks: int | None = None,
) -> dict[str, Any]:
    """Drive the middleware inside a SERVER span, mirroring the instrumentor-outside ordering."""

    def start_response(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
        pass

    with tracer.start_as_current_span("request", kind=SpanKind.SERVER):
        response: Any = middleware(environ, start_response)
        if consume_chunks is None:
            list(response)
        else:
            iterator = iter(response)
            for _ in range(consume_chunks):
                next(iterator)
        response.close()
    span: ReadableSpan
    (span,) = exporter.get_finished_spans()
    return dict(span.attributes or {})


def test_over_cap_body_sentinel_without_reading(tracer, exporter):
    configure(write_token=TOKEN, log_request_body=True)

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    environ = make_environ(method="POST", content_type="application/json", content_length="70000")
    spy = environ["wsgi.input"]
    attributes = run_request(ApitallyWSGIMiddleware(app), environ, tracer, exporter)

    assert attributes["apitally.request.body"] == BODY_TOO_LARGE
    assert attributes["http.request.body.size"] == 70000
    assert spy.read_count == 0
    assert environ["wsgi.input"] is spy


@pytest.mark.parametrize("content_length", [None, "abc"])
def test_absent_or_unparseable_content_length_means_no_capture(tracer, exporter, content_length):
    configure(write_token=TOKEN, log_request_body=True)

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    environ = make_environ(
        method="POST", body=b'{"a": 1}', content_type="application/json", content_length=content_length
    )
    spy = environ["wsgi.input"]
    attributes = run_request(ApitallyWSGIMiddleware(app), environ, tracer, exporter)

    assert "apitally.request.body" not in attributes
    assert spy.read_count == 0
    assert environ["wsgi.input"] is spy


def test_captured_body_reemitted_and_redacted(tracer, exporter):
    configure(write_token=TOKEN, log_request_body=True)
    body = b'{"password": "secret123", "item": "x"}'
    received = {}

    def app(environ, start_response):
        received["body"] = environ["wsgi.input"].read(int(environ["CONTENT_LENGTH"]))
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    environ = make_environ(method="POST", body=body, content_type="application/json", content_length=str(len(body)))
    attributes = run_request(ApitallyWSGIMiddleware(app), environ, tracer, exporter)

    assert received["body"] == body
    assert json.loads(str(attributes["apitally.request.body"])) == {"password": REDACTED, "item": "x"}


def test_redaction_failure_after_parse_fails_closed(tracer, exporter, monkeypatch):
    configure(write_token=TOKEN, log_request_body=True)
    body = b'{"a": 1}'

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    def boom(self, data):
        raise ValueError("boom")

    monkeypatch.setattr("apitally.shared.redaction.Redaction.redact_body", boom)
    environ = make_environ(method="POST", body=body, content_type="application/json", content_length=str(len(body)))
    attributes = run_request(ApitallyWSGIMiddleware(app), environ, tracer, exporter)

    assert attributes["apitally.request.body"] == REDACTED


def test_non_allowlisted_mime_never_touches_input(tracer, exporter):
    configure(write_token=TOKEN, log_request_body=True)

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    environ = make_environ(method="POST", body=b"0123456789", content_type="image/png", content_length="10")
    spy = environ["wsgi.input"]
    attributes = run_request(ApitallyWSGIMiddleware(app), environ, tracer, exporter)

    assert "apitally.request.body" not in attributes
    assert spy.read_count == 0
    assert environ["wsgi.input"] is spy


def test_response_body_accumulated_redacted_and_close_propagated(tracer, exporter):
    configure(write_token=TOKEN, log_response_body=True)
    iterable = ClosingIterable([b'{"token": "abc", ', b'"id": 1}'])

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "application/json")])
        return iterable

    attributes = run_request(ApitallyWSGIMiddleware(app), make_environ(), tracer, exporter)

    assert json.loads(str(attributes["apitally.response.body"])) == {"token": REDACTED, "id": 1}
    assert attributes["http.response.body.size"] == sum(len(c) for c in iterable.chunks)
    assert iterable.closed


def test_request_headers_redacted(tracer, exporter):
    configure(write_token=TOKEN, log_request_headers=True)

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    environ = make_environ(HTTP_AUTHORIZATION="Bearer secret123", HTTP_ACCEPT="application/json")
    attributes = run_request(ApitallyWSGIMiddleware(app), environ, tracer, exporter)

    assert attributes["http.request.header.authorization"] == [REDACTED]
    assert attributes["http.request.header.accept"] == ("application/json",)


def test_abandoned_streaming_response_leaves_size_unset(tracer, exporter):
    configure(write_token=TOKEN)

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return iter([b"one", b"two", b"three"])

    attributes = run_request(ApitallyWSGIMiddleware(app), make_environ(), tracer, exporter, consume_chunks=1)

    assert "http.response.body.size" not in attributes
