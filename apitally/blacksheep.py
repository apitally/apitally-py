import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union
from warnings import warn

from blacksheep import Application, Headers, Request, Response
from blacksheep.server.openapi.v3 import Info, OpenAPIHandler, Operation
from blacksheep.server.routing import RouteMatch

from apitally.client.client_asyncio import ApitallyClient
from apitally.client.consumers import Consumer as ApitallyConsumer
from apitally.client.request_logging import (
    BODY_TOO_LARGE,
    MAX_BODY_SIZE,
    RequestLogger,
    RequestLoggingConfig,
    RequestLoggingKwargs,
)
from apitally.common import get_versions, parse_int


try:
    from typing import Unpack
except ImportError:
    from typing_extensions import Unpack


__all__ = ["use_apitally", "ApitallyConsumer", "RequestLoggingConfig"]


def use_apitally(
    app: Application,
    client_id: str,
    env: str = "dev",
    app_version: Optional[str] = None,
    consumer_callback: Optional[Callable[[Request], Union[str, ApitallyConsumer, None]]] = None,
    identify_consumer_callback: Optional[Callable[[Request], Union[str, ApitallyConsumer, None]]] = None,
    request_logging_config: Optional[RequestLoggingConfig] = None,
    **kwargs: Unpack[RequestLoggingKwargs],
) -> None:
    """
    Use the Apitally middleware for BlackSheep applications.

    For more information, see:
    - Setup guide: https://docs.apitally.io/frameworks/blacksheep
    - Reference: https://docs.apitally.io/reference/python
    """

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

    original_get_match = app.router.get_match

    def _wrapped_router_get_match(request: Request) -> Optional[RouteMatch]:
        match = original_get_match(request)
        if match is not None:
            setattr(request, "_route_pattern", match.pattern.decode())
        return match

    app.router.get_match = _wrapped_router_get_match  # type: ignore[assignment,method-assign]

    if kwargs and request_logging_config is None:
        request_logging_config = RequestLoggingConfig.from_kwargs(kwargs)

    middleware = ApitallyMiddleware(
        app,
        client_id,
        env=env,
        request_logging_config=request_logging_config,
        app_version=app_version,
        consumer_callback=consumer_callback or identify_consumer_callback,
    )
    app.middlewares.append(middleware)


class ApitallyMiddleware:
    def __init__(
        self,
        app: Application,
        client_id: str,
        env: str = "dev",
        request_logging_config: Optional[RequestLoggingConfig] = None,
        app_version: Optional[str] = None,
        consumer_callback: Optional[Callable[[Request], Union[str, ApitallyConsumer, None]]] = None,
    ) -> None:
        self.app = app
        self.app_version = app_version
        self.consumer_callback = consumer_callback
        self.client = ApitallyClient(
            client_id=client_id,
            env=env,
            request_logging_config=request_logging_config,
        )
        self.app.on_start += self.after_start
        self.app.on_stop += self.on_stop

        self.capture_request_body = (
            self.client.request_logger.config.enabled and self.client.request_logger.config.log_request_body
        )
        self.capture_response_body = (
            self.client.request_logger.config.enabled and self.client.request_logger.config.log_response_body
        )

    async def after_start(self, application: Application) -> None:
        data = _get_startup_data(application, app_version=self.app_version)
        self.client.set_startup_data(data)
        self.client.start_sync_loop()

    async def on_stop(self, application: Application) -> None:
        await self.client.handle_shutdown()

    def get_consumer(self, request: Request) -> Optional[ApitallyConsumer]:
        identity = request.user or request.identity or None
        if identity is not None and identity.has_claim("apitally_consumer"):
            return ApitallyConsumer.from_string_or_object(identity.get("apitally_consumer"))
        if self.consumer_callback is not None:
            consumer = self.consumer_callback(request)
            return ApitallyConsumer.from_string_or_object(consumer)
        return None

    async def __call__(self, request: Request, handler: Callable[[Request], Awaitable[Response]]) -> Response:
        if not self.client.enabled or request.method.upper() == "OPTIONS":
            return await handler(request)

        timestamp = time.time()
        start_time = time.perf_counter()
        response: Optional[Response] = None
        exception: Optional[BaseException] = None

        try:
            response = await handler(request)
        except BaseException as e:
            exception = e
            raise e from None
        finally:
            response_time = time.perf_counter() - start_time

            consumer = self.get_consumer(request)
            consumer_identifier = consumer.identifier if consumer else None
            self.client.consumer_registry.add_or_update_consumer(consumer)

            route_pattern: Optional[str] = getattr(request, "_route_pattern", None)
            request_size = parse_int(request.get_first_header(b"Content-Length"))
            request_content_type = (request.content_type() or b"").decode() or None
            request_body = b""

            response_status = response.status if response else 500
            response_size: Optional[int] = None
            response_headers = Headers()
            response_body = b""

            if self.capture_request_body and RequestLogger.is_supported_content_type(request_content_type):
                if request_size is not None and request_size > MAX_BODY_SIZE:
                    request_body = BODY_TOO_LARGE
                else:
                    request_body = await request.read() or b""
                    if request_size is None:
                        request_size = len(request_body)

            if response is not None:
                response_size = (
                    response.content.length
                    if response.content
                    else parse_int(response.get_first_header(b"Content-Length"))
                )
                response_content_type = (response.content_type() or b"").decode()

                response_headers = response.headers.clone()
                if not response_headers.contains(b"Content-Type") and response.content:
                    response_headers.set(b"Content-Type", response.content.type)
                if not response_headers.contains(b"Content-Length") and response.content:
                    response_headers.set(b"Content-Length", str(response.content.length).encode())

                if self.capture_response_body and RequestLogger.is_supported_content_type(response_content_type):
                    if response_size is not None and response_size > MAX_BODY_SIZE:
                        response_body = BODY_TOO_LARGE
                    else:
                        response_body = await response.read() or b""
                        if response_size is None or response_size < 0:
                            response_size = len(response_body)

            if route_pattern:
                self.client.request_counter.add_request(
                    consumer=consumer_identifier,
                    method=request.method.upper(),
                    path=route_pattern,
                    status_code=response_status,
                    response_time=response_time,
                    request_size=request_size,
                    response_size=response_size,
                )

                if response_status == 500 and exception is not None:
                    self.client.server_error_counter.add_server_error(
                        consumer=consumer_identifier,
                        method=request.method.upper(),
                        path=route_pattern,
                        exception=exception,
                    )

            if self.client.request_logger.enabled:
                self.client.request_logger.log_request(
                    request={
                        "timestamp": timestamp,
                        "method": request.method.upper(),
                        "path": route_pattern,
                        "url": _get_full_url(request),
                        "headers": _transform_headers(request.headers),
                        "size": request_size,
                        "consumer": consumer_identifier,
                        "body": request_body,
                    },
                    response={
                        "status_code": response_status,
                        "response_time": response_time,
                        "headers": _transform_headers(response_headers),
                        "size": response_size,
                        "body": response_body,
                    },
                    exception=exception,
                )

        return response


def _get_full_url(request: Request) -> str:
    return f"{request.scheme}://{request.host}/{str(request.url).lstrip('/')}"


def _transform_headers(headers: Headers) -> List[Tuple[str, str]]:
    return [(key.decode(), value.decode()) for key, value in headers.items()]


def _get_startup_data(app: Application, app_version: Optional[str] = None) -> Dict[str, Any]:
    return {
        "paths": _get_paths(app),
        "versions": get_versions("blacksheep", app_version=app_version),
        "client": "python:blacksheep",
    }


def _get_paths(app: Application) -> List[Dict[str, str]]:
    openapi = OpenAPIHandler(info=Info(title="", version=""))
    paths = []
    methods = ("get", "put", "post", "delete", "patch")
    for path, path_item in openapi.get_paths(app).items():
        for method in methods:
            operation: Operation = getattr(path_item, method, None)
            if operation is not None:
                item = {"method": method.upper(), "path": path}
                if operation.summary:
                    item["summary"] = operation.summary
                if operation.description:
                    item["description"] = operation.description
                paths.append(item)
    return paths
