from __future__ import annotations

import io
import logging
import time
from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING, Any

from apitally.shared import metrics
from apitally.shared.capture import BODY_TOO_LARGE, MAX_BODY_SIZE, CaptureMixin, is_allowed_content_type
from apitally.shared.config import ApitallyConfig
from apitally.shared.consumer import reset_consumer_identifier, resolve_consumer_identifier
from apitally.shared.redaction import REDACTED, Redaction
from apitally.shared.span_processor import get_server_span


if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment
    from opentelemetry.sdk.trace import Span


logger = logging.getLogger(__name__)


class ApitallyWSGIMiddleware(CaptureMixin):
    """Transport-level capture middleware; must run inside the instrumentor's middleware (design.md section 6)."""

    def __init__(
        self,
        app: WSGIApplication,
        get_route: Callable[[WSGIEnvironment], str | None] | None = None,
        capture_response_body: bool = True,
    ) -> None:
        self.app = app
        self.get_route = get_route
        self.capture_response_body = capture_response_body
        self.config: ApitallyConfig | None = None
        self.redaction = Redaction()

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        config = self.refresh_config()
        state = RequestState()
        try:
            reset_consumer_identifier()
            state.request_size = parse_content_length(environ.get("CONTENT_LENGTH"))
            state.request_body = self.capture_request_body(environ, config, state.request_size)
        except Exception:
            logger.exception("Error in Apitally WSGI middleware")

        def wrapped_start_response(
            status: str, response_headers: list[tuple[str, str]], exc_info: Any = None
        ) -> Callable[[bytes], object]:
            try:
                self.handle_response_start(config, state, status, response_headers, environ)
            except Exception:
                logger.exception("Error in Apitally WSGI middleware")
            return start_response(status, response_headers, exc_info)

        return ResponseWrapper(self.app(environ, wrapped_start_response), self, environ, config, state)

    def capture_request_body(
        self, environ: WSGIEnvironment, config: ApitallyConfig, content_length: int | None
    ) -> bytes | str | None:
        if not config.log_request_body or not is_allowed_content_type(environ.get("CONTENT_TYPE")):
            return None
        if content_length is None:
            # Chunked/absent-length bodies are never read: raw-socket servers block on
            # reads past Content-Length (wsgi.md)
            return None
        if content_length > MAX_BODY_SIZE:
            return BODY_TOO_LARGE
        body = environ["wsgi.input"].read(content_length)
        environ["wsgi.input"] = io.BytesIO(body)
        return body

    def handle_response_start(
        self,
        config: ApitallyConfig,
        state: RequestState,
        status: str,
        response_headers: list[tuple[str, str]],
        environ: WSGIEnvironment,
    ) -> None:
        state.status_code = int(status.split(" ", 1)[0])
        headers = group_headers(response_headers)
        content_length = parse_content_length(next(iter(headers.get("content-length", [])), None))
        state.response_size = content_length
        if (
            self.capture_response_body
            and config.log_response_body
            and is_allowed_content_type(next(iter(headers.get("content-type", [])), None))
        ):
            over_cap = content_length is not None and content_length > MAX_BODY_SIZE
            state.response_body = BODY_TOO_LARGE if over_cap else bytearray()

        span = get_server_span()
        if span is None or not span.is_recording():
            return
        if not state.request_attributes_written:
            state.request_attributes_written = True
            if state.request_size is not None:
                span.set_attribute("http.request.body.size", state.request_size)
            if config.log_request_headers:
                self.set_header_attributes(span, "http.request.header.", environ_headers(environ))
            if state.request_body is not None:
                self.set_body_attribute(
                    span, "apitally.request.body", state.request_body, config.mask_request_body, "mask_request_body"
                )
        if content_length is not None:
            span.set_attribute("http.response.body.size", content_length)
        if config.log_response_headers:
            self.set_header_attributes(span, "http.response.header.", headers)

    def finalize(self, environ: WSGIEnvironment, config: ApitallyConfig, state: RequestState) -> None:
        if state.finalized:
            return
        state.finalized = True
        try:
            duration = time.perf_counter() - state.start_time
            span = get_server_span()
            if state.response_size is None and state.completed:
                state.response_size = state.bytes_sent
                if span is not None and span.is_recording():
                    span.set_attribute("http.response.body.size", state.response_size)
            if span is not None and span.is_recording() and state.response_body is not None:
                body = state.response_body
                if isinstance(body, bytearray):
                    # An abandoned iterable leaves a partial buffer; never export a truncated body
                    body = bytes(body) if state.completed else None
                if body is not None:
                    self.set_body_attribute(
                        span, "apitally.response.body", body, config.mask_response_body, "mask_response_body"
                    )
            route = self.get_route(environ) if self.get_route is not None else None
            metrics.record_request(
                method=environ.get("REQUEST_METHOD", ""),
                route=route or "",
                status_code=state.status_code,
                consumer=resolve_consumer_identifier(span),
                duration=duration,
                request_size=state.request_size,
                response_size=state.response_size,
                scheme=environ.get("wsgi.url_scheme"),
            )
        except Exception:
            logger.exception("Error in Apitally WSGI middleware")

    def set_header_attributes(self, span: Span, prefix: str, headers: dict[str, list[str]]) -> None:
        for name, values in headers.items():
            if self.redaction.should_redact_header(name):
                values = [REDACTED]
            span.set_attribute(prefix + name, values)


class ResponseWrapper:
    def __init__(
        self,
        response: Iterable[bytes],
        middleware: ApitallyWSGIMiddleware,
        environ: WSGIEnvironment,
        config: ApitallyConfig,
        state: RequestState,
    ) -> None:
        self.response = response
        self.iterator = iter(response)
        self.middleware = middleware
        self.environ = environ
        self.config = config
        self.state = state

    def __iter__(self) -> Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        try:
            chunk = next(self.iterator)
        except StopIteration:
            self.state.completed = True
            self.middleware.finalize(self.environ, self.config, self.state)
            raise
        try:
            self.state.bytes_sent += len(chunk)
            if isinstance(self.state.response_body, bytearray):
                self.state.response_body += chunk
                if len(self.state.response_body) > MAX_BODY_SIZE:
                    self.state.response_body = BODY_TOO_LARGE
        except Exception:
            logger.exception("Error in Apitally WSGI middleware")
        return chunk

    def close(self) -> None:
        self.middleware.finalize(self.environ, self.config, self.state)
        close = getattr(self.response, "close", None)
        if close is not None:
            close()


class RequestState:
    def __init__(self) -> None:
        self.start_time = time.perf_counter()
        self.status_code = 0
        self.request_size: int | None = None
        self.request_body: bytes | str | None = None
        self.request_attributes_written = False
        self.response_size: int | None = None
        self.response_body: bytearray | str | None = None
        self.bytes_sent = 0
        self.completed = False
        self.finalized = False


def parse_content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        length = int(value)
    except ValueError:
        return None
    return length if length >= 0 else None


def environ_headers(environ: WSGIEnvironment) -> dict[str, list[str]]:
    # WSGI collapses repeated headers into one comma-joined value, hence one list element each
    headers = {key[5:].replace("_", "-").lower(): [value] for key, value in environ.items() if key.startswith("HTTP_")}
    for key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
        if value := environ.get(key):
            headers[key.replace("_", "-").lower()] = [value]
    return headers


def group_headers(headers: list[tuple[str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for name, value in headers:
        grouped.setdefault(name.lower(), []).append(value)
    return grouped
