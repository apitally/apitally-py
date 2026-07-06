from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from opentelemetry.sdk.trace import Span

from apitally.shared import metrics
from apitally.shared.capture import ALLOWED_CONTENT_TYPES, BODY_TOO_LARGE, MAX_BODY_SIZE, CaptureMixin
from apitally.shared.consumer import reset_consumer_identifier, resolve_consumer_identifier
from apitally.shared.redaction import REDACTED
from apitally.shared.span_processor import get_server_span, is_server_span_kept


logger = logging.getLogger(__name__)

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class ApitallyASGIMiddleware(CaptureMixin):
    """Transport middleware running inside the instrumentor's SERVER span."""

    def __init__(self, app: ASGIApp, resolve_route: Callable[[Scope], str | None] | None = None) -> None:
        self.app = app
        self.resolve_route = resolve_route or resolve_route_from_scope
        self.bind_config()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        config = self.config
        start_time = time.perf_counter()
        request_size: int | None = None
        request_body = bytearray()
        request_body_length = 0
        request_body_complete = False
        request_too_large = False
        capture_request = False
        status = 0
        response_started = False
        response_size: int | None = None
        response_size_counter = 0
        response_body = bytearray()
        response_body_complete = False
        response_too_large = False
        capture_response = False
        completed = False
        kept = False

        try:
            reset_consumer_identifier()
            request_headers = scope.get("headers") or []
            request_size = parse_int(get_header(request_headers, b"content-length"))
            # Excluded and sampled-out requests skip all capture work; metrics are still recorded
            kept = is_server_span_kept()
            capture_request = (
                kept
                and config.log_request_body
                and is_supported_content_type(get_header(request_headers, b"content-type"))
            )
            request_too_large = capture_request and request_size is not None and request_size > MAX_BODY_SIZE
            span = get_server_span()
            if kept and config.log_request_headers and span is not None and span.is_recording():
                self.set_header_attributes(span, "http.request.header.", request_headers)
        except Exception:
            logger.exception("Error in Apitally ASGI middleware")

        async def receive_wrapper() -> Message:
            nonlocal request_body, request_body_length, request_body_complete, request_too_large
            message = await receive()
            try:
                if message["type"] == "http.request":
                    body = message.get("body", b"")
                    request_body_length += len(body)
                    if not request_too_large:
                        request_body += body
                        if len(request_body) > MAX_BODY_SIZE:
                            request_too_large = True
                            request_body = bytearray()
                    if not message.get("more_body", False):
                        request_body_complete = True
            except Exception:
                logger.exception("Error in Apitally ASGI middleware")
            return message

        def finish() -> None:
            nonlocal completed
            if completed:
                return
            completed = True
            duration = time.perf_counter() - start_time
            final_request_size = request_size
            if final_request_size is None and request_body_complete:
                final_request_size = request_body_length
            final_response_size = response_size
            if final_response_size is None and response_started:
                final_response_size = response_size_counter
            span = get_server_span()
            if kept and span is not None and span.is_recording():
                if final_request_size is not None:
                    span.set_attribute("http.request.body.size", final_request_size)
                if final_response_size is not None:
                    span.set_attribute("http.response.body.size", final_response_size)
                # Partial buffers from aborted requests/responses are never exported (spec 6.3)
                if request_too_large:
                    span.set_attribute("apitally.request.body", BODY_TOO_LARGE)
                elif capture_request and request_body and request_body_complete:
                    self.set_body_attribute(
                        span,
                        "apitally.request.body",
                        bytes(request_body),
                        config.mask_request_body,
                        "mask_request_body",
                    )
                if response_too_large:
                    span.set_attribute("apitally.response.body", BODY_TOO_LARGE)
                elif capture_response and response_body and response_body_complete:
                    self.set_body_attribute(
                        span,
                        "apitally.response.body",
                        bytes(response_body),
                        config.mask_response_body,
                        "mask_response_body",
                    )
            try:
                route = self.resolve_route(scope)
            except Exception:
                logger.exception("Error resolving route in Apitally ASGI middleware")
                route = None
            metrics.record_request(
                method=scope.get("method", ""),
                route=route or "",
                status_code=status,
                consumer=resolve_consumer_identifier(span),
                duration=duration,
                request_size=final_request_size,
                response_size=final_response_size,
                scheme=scope.get("scheme"),
            )

        async def send_wrapper(message: Message) -> None:
            nonlocal status, response_started, response_size, response_size_counter
            nonlocal response_body, response_body_complete, response_too_large, capture_response
            try:
                if message["type"] == "http.response.start":
                    response_started = True
                    status = message["status"]
                    response_headers = message.get("headers") or []
                    content_length = parse_int(get_header(response_headers, b"content-length"))
                    if content_length is not None and get_header(response_headers, b"transfer-encoding") != b"chunked":
                        response_size = content_length
                    capture_response = (
                        kept
                        and config.log_response_body
                        and is_supported_content_type(get_header(response_headers, b"content-type"))
                    )
                    response_too_large = (
                        capture_response and response_size is not None and response_size > MAX_BODY_SIZE
                    )
                    span = get_server_span()
                    if kept and config.log_response_headers and span is not None and span.is_recording():
                        self.set_header_attributes(span, "http.response.header.", response_headers)
                elif message["type"] == "http.response.body":
                    body = message.get("body", b"")
                    response_size_counter += len(body)
                    if capture_response and not response_too_large:
                        response_body += body
                        if len(response_body) > MAX_BODY_SIZE:
                            response_too_large = True
                            response_body = bytearray()
                    if not message.get("more_body", False):
                        # Final message: write size attributes and record metrics while the span is still recording
                        response_body_complete = True
                        finish()
            except Exception:
                logger.exception("Error in Apitally ASGI middleware")
            await send(message)

        # Zero-overhead pass-through when capture is off or the size is known to exceed the cap
        wrapped_receive = receive_wrapper if capture_request and not request_too_large else receive
        try:
            await self.app(scope, wrapped_receive, send_wrapper)
        except BaseException:
            status = status or 500
            raise
        finally:
            try:
                finish()
            except Exception:
                logger.exception("Error in Apitally ASGI middleware")

    def set_header_attributes(self, span: Span, prefix: str, headers: Iterable[tuple[bytes, bytes]]) -> None:
        grouped: dict[str, list[str]] = {}
        for name, value in headers:
            grouped.setdefault(name.decode("latin-1").lower(), []).append(value.decode("latin-1"))
        for name, values in grouped.items():
            if self.redaction.should_redact_header(name):
                values = [REDACTED]
            span.set_attribute(prefix + name, values)


def resolve_route_from_scope(scope: Scope) -> str | None:
    route = scope.get("route")
    path = getattr(route, "path", route)
    return path if isinstance(path, str) else None


def is_supported_content_type(content_type: bytes | None) -> bool:
    return content_type is not None and content_type.decode("latin-1").strip().lower().startswith(ALLOWED_CONTENT_TYPES)


def get_header(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


def parse_int(value: bytes | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.decode("latin-1"))
    except ValueError:
        return None
