import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from apitally.shared import metrics
from apitally.shared.config import (
    BODY_TOO_LARGE,
    MAX_BODY_SIZE,
    get_config,
    is_allowed_content_type,
)
from apitally.shared.consumer import get_consumer_identifier, init_consumer, reset_consumer
from apitally.shared.context import get_server_span, get_server_span_processor, is_server_span_kept


logger = logging.getLogger(__name__)

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def group_headers(headers: Iterable[tuple[bytes, bytes]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for name, value in headers:
        grouped.setdefault(name.decode("latin-1").lower(), []).append(value.decode("latin-1"))
    return grouped


class ApitallyASGIMiddleware:
    """Transport middleware. Accesses the SERVER span only in the receive/send/finish callbacks,
    so it works both inside the instrumentor's span and wrapped around the instrumented stack."""

    def __init__(self, app: ASGIApp, resolve_route: Callable[[Scope], str | None] | None = None) -> None:
        self.app = app
        self.resolve_route = resolve_route or resolve_route_from_scope
        self.config = get_config()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        config = self.config
        start_time = time.perf_counter()
        # Routing into a mounted sub-app appends the mount prefix to root_path; the value
        # saved here at request entry restores the prefix that route resolvers leave out
        initial_root_path = str(scope.get("root_path") or "")
        request_headers = scope.get("headers") or []
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
        response_headers: list[tuple[bytes, bytes]] | None = None
        response_body = bytearray()
        response_body_complete = False
        response_too_large = False
        capture_response = False
        completed = False
        deferred_span_id: int | None = None

        try:
            init_consumer()
            request_size = parse_int(get_header(request_headers, b"content-length"))
            capture_request = config.capture_request_body and is_allowed_content_type(
                get_header(request_headers, b"content-type")
            )
            request_too_large = capture_request and request_size is not None and request_size > MAX_BODY_SIZE
        except Exception:  # pragma: no cover
            logger.exception("Error in Apitally ASGI middleware")

        async def receive_wrapper() -> Message:
            nonlocal request_body, request_body_length, request_body_complete, request_too_large, capture_request
            message = await receive()
            try:
                if message["type"] == "http.request":
                    if capture_request and not is_server_span_kept():
                        capture_request = False
                        request_body = bytearray()
                    body = message.get("body", b"")
                    request_body_length += len(body)
                    if capture_request and not request_too_large:
                        request_body += body
                        if len(request_body) > MAX_BODY_SIZE:
                            request_too_large = True
                            request_body = bytearray()
                    if not message.get("more_body", False):
                        request_body_complete = True
            except Exception:  # pragma: no cover
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
            try:
                route = self.resolve_route(scope)
            except Exception:  # pragma: no cover
                logger.exception("Error resolving route in Apitally ASGI middleware")
                route = None
            if route:
                root_path = str(scope.get("root_path") or "")
                if root_path.startswith(initial_root_path):
                    route = root_path[len(initial_root_path) :] + route
            span = get_server_span()
            processor = get_server_span_processor()
            if (
                is_server_span_kept()
                and span is not None
                and span.context is not None
                and processor is not None
                and (span.is_recording() or deferred_span_id is not None)
            ):
                extra_attributes: dict[str, str | int] = {}
                if route:
                    # Overwrites the instrumentor's raw route so spans and metrics agree on the template
                    extra_attributes["http.route"] = route
                    if span.is_recording():
                        span.update_name(f"{scope.get('method', '')} {route}".strip())
                if final_request_size is not None:
                    extra_attributes["http.request.body.size"] = final_request_size
                if final_response_size is not None:
                    extra_attributes["http.response.body.size"] = final_response_size
                # Partial buffers from aborted requests/responses are never exported
                stash_request_headers = group_headers(request_headers) if config.capture_request_headers else None
                stash_response_headers = group_headers(response_headers) if response_headers is not None else None
                stash_request_body = (
                    BODY_TOO_LARGE
                    if request_too_large
                    else (bytes(request_body) if capture_request and request_body and request_body_complete else None)
                )
                stash_response_body = (
                    BODY_TOO_LARGE
                    if response_too_large
                    else (
                        bytes(response_body) if capture_response and response_body and response_body_complete else None
                    )
                )
                if stash_request_headers or stash_request_body or stash_response_headers or stash_response_body:
                    processor.update_stash(
                        span.context.span_id,
                        request_headers=stash_request_headers,
                        request_body=stash_request_body,
                        response_headers=stash_response_headers,
                        response_body=stash_response_body,
                    )
                if deferred_span_id is not None:
                    processor.finish_export(deferred_span_id, extra_attributes or None)
                else:
                    for key, value in extra_attributes.items():
                        span.set_attribute(key, value)
            metrics.record_request(
                method=scope.get("method", ""),
                route=route or "",
                status_code=status,
                consumer=get_consumer_identifier(),
                duration=duration,
                request_size=final_request_size,
                response_size=final_response_size,
                scheme=scope.get("scheme"),
            )
            reset_consumer()

        async def send_wrapper(message: Message) -> None:
            nonlocal status, response_started, response_size, response_size_counter, response_headers
            nonlocal response_body, response_body_complete, response_too_large, capture_response, deferred_span_id
            try:
                if message["type"] == "http.response.start":
                    response_started = True
                    status = message["status"]
                    headers = message.get("headers") or []
                    content_length = parse_int(get_header(headers, b"content-length"))
                    if content_length is not None and get_header(headers, b"transfer-encoding") != b"chunked":
                        response_size = content_length
                    kept = is_server_span_kept()
                    capture_response = (
                        kept
                        and config.capture_response_body
                        and is_allowed_content_type(get_header(headers, b"content-type"))
                    )
                    response_too_large = (
                        capture_response and response_size is not None and response_size > MAX_BODY_SIZE
                    )
                    if kept and config.capture_response_headers:
                        response_headers = headers
                    if kept:
                        # The instrumentor may end the span before finish runs on the exception path,
                        # so commit to a finish_export that can attach attributes after the span has ended
                        span = get_server_span()
                        processor = get_server_span_processor()
                        if span is not None and span.context is not None and processor is not None:
                            deferred_span_id = span.context.span_id
                            processor.defer_export(deferred_span_id)
                elif message["type"] == "http.response.body":
                    body = message.get("body", b"")
                    response_size_counter += len(body)
                    if capture_response and not response_too_large:
                        response_body += body
                        if len(response_body) > MAX_BODY_SIZE:
                            response_too_large = True
                            response_body = bytearray()
                    if not message.get("more_body", False):
                        response_body_complete = True
                        finish()
            except Exception:  # pragma: no cover
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
            except Exception:  # pragma: no cover
                logger.exception("Error in Apitally ASGI middleware")


def resolve_route_from_scope(scope: Scope) -> str | None:
    route = scope.get("route")
    path = getattr(route, "path", route)
    return path if isinstance(path, str) else None


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
    except ValueError:  # pragma: no cover
        return None
