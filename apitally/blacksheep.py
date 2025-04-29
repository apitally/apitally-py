import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

from blacksheep import Application, Headers, Request, Response
from blacksheep.server.openapi.v3 import OpenAPIHandler
from openapidocs.v3 import Info, Operation  # type: ignore

from apitally.client.client_asyncio import ApitallyClient
from apitally.client.consumers import Consumer as ApitallyConsumer
from apitally.client.request_logging import (
    BODY_TOO_LARGE,
    MAX_BODY_SIZE,
    RequestLogger,
    RequestLoggingConfig,
)
from apitally.common import get_versions, parse_int


__all__ = ["use_apitally", "ApitallyMiddleware", "ApitallyConsumer", "RequestLoggingConfig"]


def use_apitally(
    app: Application,
    client_id: str,
    env: str = "dev",
    request_logging_config: Optional[RequestLoggingConfig] = None,
    app_version: Optional[str] = None,
    identify_consumer_callback: Optional[Callable[[Request], Union[str, ApitallyConsumer, None]]] = None,
) -> None:
    middleware = ApitallyMiddleware(
        app,
        client_id,
        env=env,
        request_logging_config=request_logging_config,
        app_version=app_version,
        identify_consumer_callback=identify_consumer_callback,
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
        identify_consumer_callback: Optional[Callable[[Request], Union[str, ApitallyConsumer, None]]] = None,
    ) -> None:
        self.app = app
        self.identify_consumer_callback = identify_consumer_callback
        self.client = ApitallyClient(
            client_id=client_id,
            env=env,
            request_logging_config=request_logging_config,
        )
        self.client.start_sync_loop()
        self._delayed_set_startup_data_task: Optional[asyncio.Task] = None
        self.delayed_set_startup_data(app_version)
        self.app.on_stop += self.on_stop

        self.capture_request_body = (
            self.client.request_logger.config.enabled and self.client.request_logger.config.log_request_body
        )
        self.capture_response_body = (
            self.client.request_logger.config.enabled and self.client.request_logger.config.log_response_body
        )

    def delayed_set_startup_data(self, app_version: Optional[str] = None) -> None:
        self._delayed_set_startup_data_task = asyncio.create_task(self._delayed_set_startup_data(app_version))

    async def _delayed_set_startup_data(self, app_version: Optional[str] = None) -> None:
        await asyncio.sleep(1.0)  # Short delay to allow app routes to be registered first
        data: Dict[str, Any] = {}
        data["paths"] = _get_paths(self.app)
        data["versions"] = get_versions("blacksheep", app_version=app_version)
        data["client"] = "python:blacksheep"
        self.client.set_startup_data(data)

    async def on_stop(self, application: Application) -> None:
        await self.client.handle_shutdown()

    def get_consumer(self, request: Request) -> Optional[ApitallyConsumer]:
        identity = request.user or request.identity or None
        if identity is not None and identity.has_claim("apitally_consumer"):
            return ApitallyConsumer.from_string_or_object(identity.get("apitally_consumer"))
        if self.identify_consumer_callback is not None:
            consumer = self.identify_consumer_callback(request)
            return ApitallyConsumer.from_string_or_object(consumer)
        return None

    async def __call__(self, request: Request, handler: Callable[[Request], Awaitable[Response]]) -> Response:
        if not self.client.enabled:
            return await handler(request)

        timestamp = time.time()
        start_time = time.perf_counter()
        response = await handler(request)
        response_time = time.perf_counter() - start_time

        consumer = self.get_consumer(request)
        consumer_identifier = consumer.identifier if consumer else None
        self.client.consumer_registry.add_or_update_consumer(consumer)

        request_size = parse_int(request.get_first_header(b"Content-Length"))
        response_size = parse_int(response.get_first_header(b"Content-Length"))

        if request.method.upper() != "OPTIONS":
            self.client.request_counter.add_request(
                consumer=consumer_identifier,
                method=request.method.upper(),
                path=request.path,
                status_code=response.status,
                response_time=response_time,
                request_size=request_size,
                response_size=response_size,
            )

        if self.client.request_logger.enabled:
            response_body = b""
            if self.capture_response_body and RequestLogger.is_supported_content_type(response.content_type().decode()):
                if response_size is not None and response_size > MAX_BODY_SIZE:
                    response_body = BODY_TOO_LARGE
                else:
                    response_body = await response.read() or b""

            self.client.request_logger.log_request(
                request={
                    "timestamp": timestamp,
                    "method": request.method.upper(),
                    "path": request.path,
                    "url": str(request.url),
                    "headers": _transform_headers(request.headers),
                    "size": request_size,
                    "consumer": consumer_identifier,
                    "body": b"",
                },
                response={
                    "status_code": response.status,
                    "response_time": response_time,
                    "headers": _transform_headers(response.headers),
                    "size": response_size,
                    "body": response_body,
                },
                # exception=exception,
            )

        return response


def _get_paths(app: Application) -> List[Dict[str, str]]:
    openapi = OpenAPIHandler(info=Info(title="", version=""))
    paths = []
    methods = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
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


def _transform_headers(headers: Headers) -> List[Tuple[str, str]]:
    return [(key.decode(), value.decode()) for key, value in headers.items()]
