from __future__ import annotations

import json
import sys
import time
from importlib.metadata import version
from typing import Callable, Dict, Optional

from litestar.app import DEFAULT_OPENAPI_CONFIG, Litestar
from litestar.config.app import AppConfig
from litestar.connection import Request
from litestar.datastructures import Headers
from litestar.enums import ScopeType
from litestar.plugins import InitPluginProtocol
from litestar.types import ASGIApp, Message, Receive, Scope, Send

from apitally.client.asyncio import ApitallyClient


__all__ = ["ApitallyPlugin"]


class ApitallyPlugin(InitPluginProtocol):
    def __init__(
        self,
        client_id: str,
        env: str = "dev",
        app_version: Optional[str] = None,
        identify_consumer_callback: Optional[Callable[[Request], Optional[str]]] = None,
    ) -> None:
        self.client: ApitallyClient = ApitallyClient(client_id=client_id, env=env)
        self.app_version = app_version
        self.identify_consumer_callback = identify_consumer_callback

    def on_app_init(self, app_config: AppConfig) -> AppConfig:
        app_config.on_startup.append(self.on_startup)
        app_config.middleware.append(self.middleware_factory)
        app_config.after_request
        return app_config

    def on_startup(self, app: Litestar) -> None:
        app_info = {
            "openapi": _get_openapi(app),
            "paths": _get_paths(app),
            "versions": _get_versions(self.app_version),
            "client": "python:litestar",
        }
        self.client.set_app_info(app_info)
        self.client.start_sync_loop()

    def middleware_factory(self, app: ASGIApp) -> ASGIApp:
        async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http" and scope["method"] != "OPTIONS":
                request = Request(scope)
                start_time = time.perf_counter()
                response_status = 0
                response_time = 0.0
                response_headers = Headers()
                response_body = b""

                async def send_wrapper(message: Message) -> None:
                    nonlocal response_time, response_status, response_headers, response_body
                    if message["type"] == "http.response.start":
                        response_time = time.perf_counter() - start_time
                        response_status = message["status"]
                        response_headers = Headers(message["headers"])
                    elif message["type"] == "http.response.body" and response_status == 400:
                        response_body += message["body"]
                    await send(message)

                await app(scope, receive, send_wrapper)
                self.add_request(
                    request=request,
                    response_status=response_status,
                    response_time=response_time,
                    response_headers=response_headers,
                    response_body=response_body,
                )
            else:
                await app(scope, receive, send)  # pragma: no cover

        return middleware

    def add_request(
        self,
        request: Request,
        response_status: int,
        response_time: float,
        response_headers: Headers,
        response_body: bytes,
    ) -> None:
        if not request.route_handler.paths:
            return  # pragma: no cover
        consumer = self.get_consumer(request)
        path = list(request.route_handler.paths)[0]
        self.client.request_counter.add_request(
            consumer=consumer,
            method=request.method,
            path=path,
            status_code=response_status,
            response_time=response_time,
            request_size=request.headers.get("Content-Length"),
            response_size=response_headers.get("Content-Length"),
        )
        if response_status == 400 and response_body and len(response_body) < 4096:
            try:
                parsed_body = json.loads(response_body)
            except json.JSONDecodeError:  # pragma: no cover
                pass
            else:
                if (
                    isinstance(parsed_body, dict)
                    and "detail" in parsed_body
                    and isinstance(parsed_body["detail"], str)
                    and "validation" in parsed_body["detail"].lower()
                    and "extra" in parsed_body
                    and isinstance(parsed_body["extra"], list)
                ):
                    self.client.validation_error_counter.add_validation_errors(
                        consumer=consumer,
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

    def get_consumer(self, request: Request) -> Optional[str]:
        if hasattr(request.state, "consumer_identifier"):
            return str(request.state.consumer_identifier)
        if self.identify_consumer_callback is not None:
            consumer_identifier = self.identify_consumer_callback(request)
            if consumer_identifier is not None:
                return str(consumer_identifier)
        return None


def _get_openapi(app: Litestar) -> str:
    schema = app.openapi_schema.to_schema()
    return json.dumps(schema)


def _get_paths(app: Litestar) -> list[dict[str, str]]:
    openapi_config = app.openapi_config or DEFAULT_OPENAPI_CONFIG
    schema_path = openapi_config.openapi_controller.path
    return [
        {"method": method.upper(), "path": route.path}
        for route in app.routes
        for method in route.methods
        if route.scope_type == ScopeType.HTTP
        and method.upper() != "OPTIONS"
        and route.path != schema_path
        and not route.path.startswith(schema_path + "/")
    ]


def _get_versions(app_version: Optional[str]) -> Dict[str, str]:
    versions = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "apitally": version("apitally"),
        "litestar": version("litestar"),
    }
    if app_version:
        versions["app"] = app_version
    return versions
