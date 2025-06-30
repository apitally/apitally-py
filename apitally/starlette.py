from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union
from warnings import warn

from httpx import HTTPStatusError, Proxy
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.routing import BaseRoute, Match, Router
from starlette.schemas import EndpointInfo, SchemaGenerator
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Lifespan, Message, Receive, Scope, Send

from apitally.client.client_asyncio import ApitallyClient
from apitally.client.consumers import Consumer as ApitallyConsumer
from apitally.client.request_logging import (
    BODY_TOO_LARGE,
    MAX_BODY_SIZE,
    RequestLogger,
    RequestLoggingConfig,
    RequestLoggingKwargs,
)
from apitally.common import get_versions, parse_int, try_json_loads


try:
    from typing import Unpack
except ImportError:
    from typing_extensions import Unpack


__all__ = ["ApitallyMiddleware", "ApitallyConsumer", "RequestLoggingConfig", "set_consumer"]


class ApitallyMiddleware:
    """
    Apitally middleware for Starlette applications.

    For more information, see:
    - Setup guide: https://docs.apitally.io/frameworks/starlette
    - Reference: https://docs.apitally.io/reference/python
    """

    def __init__(
        self,
        app: ASGIApp,
        client_id: str,
        env: str = "dev",
        app_version: Optional[str] = None,
        openapi_url: Optional[str] = "/openapi.json",
        consumer_callback: Optional[Callable[[Request], Union[str, ApitallyConsumer, None]]] = None,
        capture_client_disconnects: bool = False,
        proxy: Optional[Union[str, Proxy]] = None,
        identify_consumer_callback: Optional[Callable[[Request], Union[str, ApitallyConsumer, None]]] = None,
        request_logging_config: Optional[RequestLoggingConfig] = None,
        **kwargs: Unpack[RequestLoggingKwargs],
    ) -> None:
        if identify_consumer_callback is not None:
            warn(
                "The 'identify_consumer_callback' parameter is deprecated, use 'consumer_callback' instead.",
                DeprecationWarning,
                stacklevel=2,
            )
        if request_logging_config is not None:
            warn(
                "The 'request_logging_config' parameter is deprecated, use keyword arguments instead.",
                DeprecationWarning,
                stacklevel=2,
            )

        self.app = app
        self.app_version = app_version
        self.openapi_url = openapi_url
        self.consumer_callback = consumer_callback or identify_consumer_callback
        self.capture_client_disconnects = capture_client_disconnects

        if kwargs and request_logging_config is None:
            request_logging_config = RequestLoggingConfig.from_kwargs(kwargs)

        self.client = ApitallyClient(
            client_id=client_id,
            env=env,
            request_logging_config=request_logging_config,
            proxy=proxy,
        )

        self.capture_request_body = (
            self.client.request_logger.config.enabled and self.client.request_logger.config.log_request_body
        )
        self.capture_response_body = (
            self.client.request_logger.config.enabled and self.client.request_logger.config.log_response_body
        )

        _inject_lifespan_handlers(
            app,
            on_startup=self.on_startup,
            on_shutdown=self.client.handle_shutdown,
        )

    async def on_startup(self) -> None:
        data = _get_startup_data(self.app, app_version=self.app_version, openapi_url=self.openapi_url)
        self.client.set_startup_data(data)
        self.client.start_sync_loop()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.client.enabled and scope["type"] == "http" and scope["method"] != "OPTIONS":
            timestamp = time.time()
            request = Request(scope, receive, send)
            request_size = parse_int(request.headers.get("Content-Length"))
            request_body = b""
            request_body_too_large = request_size is not None and request_size > MAX_BODY_SIZE
            response_status = 0
            response_time: Optional[float] = None
            response_headers = Headers()
            response_body = b""
            response_body_too_large = False
            response_size: Optional[int] = None
            response_chunked = False
            response_content_type: Optional[str] = None
            exception: Optional[BaseException] = None
            start_time = time.perf_counter()

            async def receive_wrapper() -> Message:
                nonlocal request_body, request_body_too_large

                message = await receive()
                if message["type"] == "http.request" and self.capture_request_body and not request_body_too_large:
                    request_body += message.get("body", b"")
                    if len(request_body) > MAX_BODY_SIZE:
                        request_body_too_large = True
                        request_body = b""
                return message

            async def send_wrapper(message: Message) -> None:
                nonlocal \
                    response_time, \
                    response_status, \
                    response_headers, \
                    response_body, \
                    response_body_too_large, \
                    response_chunked, \
                    response_content_type, \
                    response_size

                if message["type"] == "http.response.start":
                    response_time = time.perf_counter() - start_time
                    response_status = message["status"]
                    response_headers = Headers(scope=message)
                    response_chunked = (
                        response_headers.get("Transfer-Encoding") == "chunked"
                        or "Content-Length" not in response_headers
                    )
                    response_content_type = response_headers.get("Content-Type")
                    response_size = parse_int(response_headers.get("Content-Length")) if not response_chunked else 0
                    response_body_too_large = response_size is not None and response_size > MAX_BODY_SIZE

                elif message["type"] == "http.response.body":
                    if response_chunked and response_size is not None:
                        response_size += len(message.get("body", b""))

                    if (
                        (self.capture_response_body or response_status == 422)
                        and RequestLogger.is_supported_content_type(response_content_type)
                        and not response_body_too_large
                    ):
                        response_body += message.get("body", b"")
                        if len(response_body) > MAX_BODY_SIZE:
                            response_body_too_large = True
                            response_body = b""

                if self.capture_client_disconnects and await request.is_disconnected():
                    # Client closed connection (report NGINX specific status code)
                    response_status = 499

                await send(message)

            try:
                await self.app(scope, receive_wrapper, send_wrapper)
            except BaseException as e:
                exception = e
                raise e from None
            finally:
                if response_time is None:
                    response_time = time.perf_counter() - start_time
                self.add_request(
                    timestamp=timestamp,
                    request=request,
                    request_body=request_body if not request_body_too_large else BODY_TOO_LARGE,
                    request_size=request_size,
                    response_status=response_status,
                    response_time=response_time,
                    response_headers=response_headers,
                    response_body=response_body if not response_body_too_large else BODY_TOO_LARGE,
                    response_size=response_size,
                    exception=exception,
                )
        else:
            await self.app(scope, receive, send)  # pragma: no cover

    def add_request(
        self,
        timestamp: float,
        request: Request,
        request_body: bytes,
        request_size: Optional[int],
        response_status: int,
        response_time: float,
        response_headers: Headers,
        response_body: bytes,
        response_size: Optional[int],
        exception: Optional[BaseException] = None,
    ) -> None:
        path = self.get_path(request)

        consumer = self.get_consumer(request)
        consumer_identifier = consumer.identifier if consumer else None
        self.client.consumer_registry.add_or_update_consumer(consumer)

        if path is not None:
            if response_status == 0 and exception is not None:
                response_status = 500
            self.client.request_counter.add_request(
                consumer=consumer_identifier,
                method=request.method,
                path=path,
                status_code=response_status,
                response_time=response_time,
                request_size=request_size,
                response_size=response_size,
            )
            if response_status == 422 and response_body and response_headers.get("Content-Type") == "application/json":
                body = try_json_loads(response_body, encoding=response_headers.get("Content-Encoding"))
                if isinstance(body, dict) and "detail" in body and isinstance(body["detail"], list):
                    # Log FastAPI / Pydantic validation errors
                    self.client.validation_error_counter.add_validation_errors(
                        consumer=consumer_identifier,
                        method=request.method,
                        path=path,
                        detail=body["detail"],
                    )
            if response_status == 500 and exception is not None:
                self.client.server_error_counter.add_server_error(
                    consumer=consumer_identifier,
                    method=request.method,
                    path=path,
                    exception=exception,
                )

        if self.client.request_logger.enabled:
            self.client.request_logger.log_request(
                request={
                    "timestamp": timestamp,
                    "method": request.method,
                    "path": path,
                    "url": str(request.url),
                    "headers": request.headers.items(),
                    "size": request_size,
                    "consumer": consumer_identifier,
                    "body": request_body,
                },
                response={
                    "status_code": response_status,
                    "response_time": response_time,
                    "headers": response_headers.items(),
                    "size": response_size,
                    "body": response_body,
                },
                exception=exception,
            )

    def get_path(self, request: Request, routes: Optional[list[BaseRoute]] = None) -> Optional[str]:
        if routes is None:
            routes = request.app.routes
        for route in routes:
            if hasattr(route, "routes"):
                path = self.get_path(request, routes=route.routes)
                if path is not None:
                    return path
            elif hasattr(route, "path"):
                match, _ = route.matches(request.scope)
                if match == Match.FULL:
                    return request.scope.get("root_path", "") + route.path
        return None

    def get_consumer(self, request: Request) -> Optional[ApitallyConsumer]:
        if hasattr(request.state, "apitally_consumer") and request.state.apitally_consumer:
            return ApitallyConsumer.from_string_or_object(request.state.apitally_consumer)
        if hasattr(request.state, "consumer_identifier") and request.state.consumer_identifier:
            # Keeping this for legacy support
            warn(
                "Providing a consumer identifier via `request.state.consumer_identifier` is deprecated, "
                "use `request.state.apitally_consumer` instead.",
                DeprecationWarning,
            )
            return ApitallyConsumer.from_string_or_object(request.state.consumer_identifier)
        if self.consumer_callback is not None:
            consumer = self.consumer_callback(request)
            return ApitallyConsumer.from_string_or_object(consumer)
        return None


def set_consumer(request: Request, identifier: str, name: Optional[str] = None, group: Optional[str] = None) -> None:
    request.state.apitally_consumer = ApitallyConsumer(identifier, name=name, group=group)


def _get_startup_data(
    app: ASGIApp, app_version: Optional[str] = None, openapi_url: Optional[str] = None
) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if openapi_url and (openapi := _get_openapi(app, openapi_url)):
        data["openapi"] = openapi
    if endpoints := _get_endpoint_info(app):
        data["paths"] = [{"path": endpoint.path, "method": endpoint.http_method} for endpoint in endpoints]
    data["versions"] = get_versions("fastapi", "starlette", app_version=app_version)
    data["client"] = "python:starlette"
    return data


def _get_openapi(app: ASGIApp, openapi_url: str) -> Optional[str]:
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(openapi_url)
        response.raise_for_status()
        return response.text
    except HTTPStatusError:
        return None


def _get_endpoint_info(app: ASGIApp) -> List[EndpointInfo]:
    routes = _get_routes(app)
    schemas = SchemaGenerator({})
    return schemas.get_endpoints(routes)


def _get_routes(app: Union[ASGIApp, Router]) -> List[BaseRoute]:
    if isinstance(app, Router):
        return app.routes
    elif hasattr(app, "app"):
        return _get_routes(app.app)
    return []  # pragma: no cover


def _inject_lifespan_handlers(
    app: Union[ASGIApp, Router],
    on_startup: Callable[[], Awaitable[Any]],
    on_shutdown: Callable[[], Awaitable[Any]],
) -> None:
    """
    Ensures the given startup and shutdown functions are called as part of the app's lifespan context manager.
    """
    router = app
    while not isinstance(router, Router) and hasattr(router, "app"):
        router = router.app
    if not isinstance(router, Router):
        raise TypeError("app must be a Starlette or Router instance")

    lifespan: Optional[Lifespan] = getattr(router, "lifespan_context", None)

    @asynccontextmanager
    async def wrapped_lifespan(app: Starlette):
        await on_startup()
        if lifespan is not None:
            async with lifespan(app):
                yield
        else:
            yield
        await on_shutdown()

    router.lifespan_context = wrapped_lifespan
