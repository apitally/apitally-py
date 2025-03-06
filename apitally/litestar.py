import contextlib
import json
import time
from typing import Callable, Dict, List, Optional, Union
from warnings import warn

from httpx import Proxy
from litestar.app import DEFAULT_OPENAPI_CONFIG, Litestar
from litestar.config.app import AppConfig
from litestar.connection import Request
from litestar.datastructures import Headers
from litestar.enums import ScopeType
from litestar.handlers import HTTPRouteHandler
from litestar.plugins import InitPluginProtocol
from litestar.types import ASGIApp, Message, Receive, Scope, Send

from apitally.client.client_asyncio import ApitallyClient
from apitally.client.consumers import Consumer as ApitallyConsumer
from apitally.client.request_logging import (
    BODY_TOO_LARGE,
    MAX_BODY_SIZE,
    RequestLogger,
    RequestLoggingConfig,
)
from apitally.common import get_versions, parse_int


__all__ = ["ApitallyPlugin", "ApitallyConsumer", "RequestLoggingConfig"]


class ApitallyPlugin(InitPluginProtocol):
    def __init__(
        self,
        client_id: str,
        env: str = "dev",
        request_logging_config: Optional[RequestLoggingConfig] = None,
        app_version: Optional[str] = None,
        filter_openapi_paths: bool = True,
        identify_consumer_callback: Optional[Callable[[Request], Union[str, ApitallyConsumer, None]]] = None,
        proxy: Optional[Union[str, Proxy]] = None,
    ) -> None:
        self.client = ApitallyClient(
            client_id=client_id,
            env=env,
            request_logging_config=request_logging_config,
            proxy=proxy,
        )
        self.app_version = app_version
        self.filter_openapi_paths = filter_openapi_paths
        self.identify_consumer_callback = identify_consumer_callback

        self.openapi_path = "/schema"
        self.capture_request_body = (
            self.client.request_logger.config.enabled and self.client.request_logger.config.log_request_body
        )
        self.capture_response_body = (
            self.client.request_logger.config.enabled and self.client.request_logger.config.log_response_body
        )

    def on_app_init(self, app_config: AppConfig) -> AppConfig:
        app_config.on_startup.append(self.on_startup)
        app_config.on_shutdown.append(self.client.handle_shutdown)
        app_config.middleware.append(self.middleware_factory)
        app_config.after_exception.append(self.after_exception)
        return app_config

    def on_startup(self, app: Litestar) -> None:
        openapi_config = app.openapi_config or DEFAULT_OPENAPI_CONFIG
        if openapi_config.openapi_controller is not None:
            self.openapi_path = openapi_config.openapi_controller.path
        elif hasattr(openapi_config, "openapi_router") and openapi_config.openapi_router is not None:
            self.openapi_path = openapi_config.openapi_router.path
        elif openapi_config.path is not None:
            self.openapi_path = openapi_config.path

        data = {
            "openapi": _get_openapi(app),
            "paths": [route for route in _get_routes(app) if not self.filter_path(route["path"])],
            "versions": get_versions("litestar", app_version=self.app_version),
            "client": "python:litestar",
        }
        self.client.set_startup_data(data)
        self.client.start_sync_loop()

    def after_exception(self, exception: Exception, scope: Scope) -> None:
        scope["state"]["exception"] = exception

    def middleware_factory(self, app: ASGIApp) -> ASGIApp:
        async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
            if self.client.enabled and scope["type"] == "http" and scope["method"] != "OPTIONS":
                timestamp = time.time()
                request = Request(scope)
                request_size = parse_int(request.headers.get("Content-Length"))
                request_body = b""
                request_body_too_large = request_size is not None and request_size > MAX_BODY_SIZE
                response_status = 0
                response_time = 0.0
                response_headers = Headers()
                response_body = b""
                response_body_too_large = False
                response_size: Optional[int] = None
                response_chunked = False
                response_content_type: Optional[str] = None
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
                        response_headers = Headers(message["headers"])
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
                            (self.capture_response_body or response_status == 400)
                            and RequestLogger.is_supported_content_type(response_content_type)
                            and not response_body_too_large
                        ):
                            response_body += message.get("body", b"")
                            if len(response_body) > MAX_BODY_SIZE:
                                response_body_too_large = True
                                response_body = b""
                    await send(message)

                await app(scope, receive_wrapper, send_wrapper)
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
                )
            else:
                await app(scope, receive, send)  # pragma: no cover

        return middleware

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
    ) -> None:
        if response_status < 100:
            return  # pragma: no cover
        path = self.get_path(request)
        if self.filter_path(path):
            return

        consumer = self.get_consumer(request)
        consumer_identifier = consumer.identifier if consumer else None
        self.client.consumer_registry.add_or_update_consumer(consumer)

        if path is not None:
            self.client.request_counter.add_request(
                consumer=consumer_identifier,
                method=request.method,
                path=path,
                status_code=response_status,
                response_time=response_time,
                request_size=request_size,
                response_size=response_size,
            )

            if response_status == 400 and response_body and len(response_body) < 4096:
                with contextlib.suppress(json.JSONDecodeError):
                    parsed_body = json.loads(response_body)
                    if (
                        isinstance(parsed_body, dict)
                        and "detail" in parsed_body
                        and isinstance(parsed_body["detail"], str)
                        and "validation" in parsed_body["detail"].lower()
                        and "extra" in parsed_body
                        and isinstance(parsed_body["extra"], list)
                    ):
                        self.client.validation_error_counter.add_validation_errors(
                            consumer=consumer_identifier,
                            method=request.method,
                            path=path,
                            detail=[
                                {
                                    "loc": [error.get("source", "body")] + error["key"].split("."),
                                    "msg": error["message"],
                                    "type": "",
                                }
                                for error in parsed_body["extra"]
                                if "key" in error and "message" in error
                            ],
                        )

            if response_status == 500 and "exception" in request.state:
                self.client.server_error_counter.add_server_error(
                    consumer=consumer_identifier,
                    method=request.method,
                    path=path,
                    exception=request.state["exception"],
                )

        if self.client.request_logger.enabled:
            self.client.request_logger.log_request(
                request={
                    "timestamp": timestamp,
                    "method": request.method,
                    "path": path,
                    "url": str(request.url),
                    "headers": [(k, v) for k, v in request.headers.items()],
                    "size": request_size,
                    "consumer": consumer_identifier,
                    "body": request_body,
                },
                response={
                    "status_code": response_status,
                    "response_time": response_time,
                    "headers": [(k, v) for k, v in response_headers.items()],
                    "size": response_size,
                    "body": response_body,
                },
            )

    def get_path(self, request: Request) -> Optional[str]:
        if not request.route_handler.paths:
            return None
        path: List[str] = []
        for layer in request.route_handler.ownership_layers:
            if isinstance(layer, HTTPRouteHandler):
                if len(layer.paths) == 0:
                    return None  # pragma: no cover
                path.append(list(layer.paths)[0].lstrip("/"))
            else:
                path.append(layer.path.lstrip("/"))
        return "/" + "/".join(filter(None, path))

    def filter_path(self, path: Optional[str]) -> bool:
        if path is not None and self.filter_openapi_paths and self.openapi_path:
            return path == self.openapi_path or path.startswith(self.openapi_path + "/")
        return False  # pragma: no cover

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
        if self.identify_consumer_callback is not None:
            consumer = self.identify_consumer_callback(request)
            return ApitallyConsumer.from_string_or_object(consumer)
        return None


def _get_openapi(app: Litestar) -> str:
    schema = app.openapi_schema.to_schema()
    return json.dumps(schema)


def _get_routes(app: Litestar) -> List[Dict[str, str]]:
    return [
        {"method": method, "path": route.path}
        for route in app.routes
        for method in route.methods
        if route.scope_type == ScopeType.HTTP and method != "OPTIONS"
    ]
