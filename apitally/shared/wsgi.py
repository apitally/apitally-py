from __future__ import annotations

import io
import logging
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from apitally.shared import metrics
from apitally.shared.capture import BODY_TOO_LARGE, MAX_BODY_SIZE, CaptureMixin, is_allowed_content_type
from apitally.shared.config import ApitallyConfig
from apitally.shared.consumer import get_consumer_identifier, reset_consumer
from apitally.shared.span_processor import get_server_span, get_server_span_processor, is_server_span_kept


if TYPE_CHECKING:
    from _typeshed import OptExcInfo
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment


logger = logging.getLogger(__name__)


class ApitallyWSGIMiddleware(CaptureMixin):
    """Transport-level capture middleware. Must run inside the instrumentor's middleware."""

    def __init__(
        self,
        app: WSGIApplication,
        get_route: Callable[[WSGIEnvironment], str | None] | None = None,
        capture_response_body: bool = True,
    ) -> None:
        self.app = app
        self.get_route = get_route
        self.capture_response_body = capture_response_body
        self.bind_config()

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        config = self.config
        state = RequestState()
        try:
            reset_consumer()
            state.request_size = parse_content_length(environ.get("CONTENT_LENGTH"))
            state.request_body = self.capture_request_body(environ, config, state.request_size)
        except Exception:  # pragma: no cover
            logger.exception("Error in Apitally WSGI middleware")

        def wrapped_start_response(
            status: str, response_headers: list[tuple[str, str]], exc_info: OptExcInfo | None = None
        ) -> Callable[[bytes], object]:
            try:
                self.handle_response_start(config, state, status, response_headers, environ)
            except Exception:  # pragma: no cover
                logger.exception("Error in Apitally WSGI middleware")
            return start_response(status, response_headers, exc_info)

        try:
            response = self.app(environ, wrapped_start_response)
        except BaseException:
            # The app raised after start_response; finalize never runs, so release the deferral
            if state.deferred_span_id is not None and (processor := get_server_span_processor()) is not None:
                processor.finish_export(state.deferred_span_id)
            raise
        return ResponseWrapper(response, self, environ, config, state)

    def capture_request_body(
        self, environ: WSGIEnvironment, config: ApitallyConfig, content_length: int | None
    ) -> bytes | str | None:
        # The keep decision is not checked here: on Flask the SERVER span starts later, in
        # before_request. handle_response_start checks it and only then writes the buffered body.
        if not config.log_request_body or not is_allowed_content_type(environ.get("CONTENT_TYPE")):
            return None
        if content_length is None:
            # Chunked/absent-length bodies are never read: raw-socket servers block on
            # reads past Content-Length
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
        kept = is_server_span_kept()
        if (
            kept
            and self.capture_response_body
            and config.log_response_body
            and is_allowed_content_type(next(iter(headers.get("content-type", [])), None))
        ):
            over_cap = content_length is not None and content_length > MAX_BODY_SIZE
            state.response_body = BODY_TOO_LARGE if over_cap else bytearray()

        span = get_server_span()
        if not kept or span is None or not span.is_recording():
            return
        processor = get_server_span_processor()
        stash_request_headers: dict[str, list[str]] | None = None
        stash_request_body: bytes | None = None
        if not state.request_attributes_written:
            state.request_attributes_written = True
            if state.request_size is not None:
                span.set_attribute("http.request.body.size", state.request_size)
            if state.request_body is BODY_TOO_LARGE:
                span.set_attribute("apitally.request.body", BODY_TOO_LARGE)
            elif isinstance(state.request_body, bytes):
                stash_request_body = state.request_body
            if config.log_request_headers:
                stash_request_headers = environ_headers(environ)
        stash_response_headers = headers if config.log_response_headers else None
        if processor is not None and span.context is not None:
            if stash_request_headers or stash_request_body or stash_response_headers:
                processor.update_stash(
                    span.context.span_id,
                    request_headers=stash_request_headers,
                    request_body=stash_request_body,
                    response_headers=stash_response_headers,
                )
            # The final response size is only known at finalize, which may run after the span has ended
            processor.defer_export(span.context.span_id)
            state.deferred_span_id = span.context.span_id

    def finalize(self, environ: WSGIEnvironment, config: ApitallyConfig, state: RequestState) -> None:
        if state.finalized:
            return
        state.finalized = True
        try:
            duration = time.perf_counter() - state.start_time
            span = get_server_span()
            kept = is_server_span_kept()
            if state.response_size is None and state.completed:
                state.response_size = state.bytes_sent
            extra: dict[str, str | int] = {}
            if state.response_size is not None:
                extra["http.response.body.size"] = state.response_size
            processor = get_server_span_processor() if state.deferred_span_id is not None else None
            if kept and span is not None and state.response_body is not None:
                body = state.response_body
                if isinstance(body, bytearray):
                    # An abandoned iterable leaves a partial buffer; never export a truncated body
                    body = bytes(body) if state.completed else None
                if body is BODY_TOO_LARGE:
                    extra["apitally.response.body"] = BODY_TOO_LARGE
                elif isinstance(body, bytes) and processor is not None and state.deferred_span_id is not None:
                    # The deferred export guarantees process_ended_span still runs and attaches this body
                    processor.update_stash(state.deferred_span_id, response_body=body)
            if state.deferred_span_id is not None and processor is not None:
                processor.finish_export(state.deferred_span_id, extra or None)
            route = self.get_route(environ) if self.get_route is not None else None
            metrics.record_request(
                method=environ.get("REQUEST_METHOD", ""),
                route=route or "",
                status_code=state.status_code,
                consumer=get_consumer_identifier(),
                duration=duration,
                request_size=state.request_size,
                response_size=state.response_size,
                scheme=environ.get("wsgi.url_scheme"),
            )
        except Exception:  # pragma: no cover
            logger.exception("Error in Apitally WSGI middleware")


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
        except Exception:  # pragma: no cover
            logger.exception("Error in Apitally WSGI middleware")
        return chunk

    def close(self) -> None:
        self.middleware.finalize(self.environ, self.config, self.state)
        close = getattr(self.response, "close", None)
        if close is not None:
            close()


@dataclass(slots=True)
class RequestState:
    start_time: float = field(default_factory=time.perf_counter)
    status_code: int = 0
    request_size: int | None = None
    request_body: bytes | str | None = None
    request_attributes_written: bool = False
    response_size: int | None = None
    response_body: bytearray | str | None = None
    bytes_sent: int = 0
    completed: bool = False
    finalized: bool = False
    deferred_span_id: int | None = None


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


def group_headers(headers: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for name, value in headers:
        grouped.setdefault(name.lower(), []).append(value)
    return grouped
