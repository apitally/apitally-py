from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from warnings import warn

from httpx import HTTPStatusError
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.routing import BaseRoute, Match, Router
from starlette.schemas import EndpointInfo, SchemaGenerator
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from apitally.client.asyncio import ApitallyClient
from apitally.client.base import Consumer as ApitallyConsumer
from apitally.common import get_versions


__all__ = ["ApitallyMiddleware", "ApitallyConsumer"]


class ApitallyMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        client_id: str,
        env: str = "dev",
        app_version: Optional[str] = None,
        openapi_url: Optional[str] = "/openapi.json",
        filter_unhandled_paths: bool = True,
        identify_consumer_callback: Optional[Callable[[Request], Union[str, ApitallyConsumer, None]]] = None,
    ) -> None:
        self.app = app
        self.filter_unhandled_paths = filter_unhandled_paths
        self.identify_consumer_callback = identify_consumer_callback
        self.client = ApitallyClient(client_id=client_id, env=env)
        self.client.start_sync_loop()
        self._delayed_set_startup_data_task: Optional[asyncio.Task] = None
        self.delayed_set_startup_data(app_version, openapi_url)
        _register_shutdown_handler(app, self.client.handle_shutdown)

    def delayed_set_startup_data(self, app_version: Optional[str] = None, openapi_url: Optional[str] = None) -> None:
        self._delayed_set_startup_data_task = asyncio.create_task(
            self._delayed_set_startup_data(app_version, openapi_url)
        )

    async def _delayed_set_startup_data(
        self, app_version: Optional[str] = None, openapi_url: Optional[str] = None
    ) -> None:
        await asyncio.sleep(1.0)  # Short delay to allow app routes to be registered first
        data = _get_startup_data(self.app, app_version, openapi_url)
        self.client.set_startup_data(data)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] != "OPTIONS":
            request = Request(scope)
            response_status = 0
            response_time = 0.0
            response_headers = Headers()
            response_body = b""
            start_time = time.perf_counter()

            async def send_wrapper(message: Message) -> None:
                nonlocal response_time, response_status, response_headers, response_body
                if message["type"] == "http.response.start":
                    response_time = time.perf_counter() - start_time
                    response_status = message["status"]
                    response_headers = Headers(scope=message)
                elif message["type"] == "http.response.body" and response_status == 422:
                    response_body += message["body"]
                await send(message)

            try:
                await self.app(scope, receive, send_wrapper)
            except BaseException as e:
                self.add_request(
                    request=request,
                    response_status=500,
                    response_time=time.perf_counter() - start_time,
                    response_headers=response_headers,
                    response_body=response_body,
                    exception=e,
                )
                raise e from None
            else:
                self.add_request(
                    request=request,
                    response_status=response_status,
                    response_time=response_time,
                    response_headers=response_headers,
                    response_body=response_body,
                )
        else:
            await self.app(scope, receive, send)  # pragma: no cover

    def add_request(
        self,
        request: Request,
        response_status: int,
        response_time: float,
        response_headers: Headers,
        response_body: bytes,
        exception: Optional[BaseException] = None,
    ) -> None:
        path_template, is_handled_path = self.get_path_template(request)
        if is_handled_path or not self.filter_unhandled_paths:
            consumer = self.get_consumer(request)
            consumer_identifier = consumer.identifier if consumer else None
            self.client.consumer_registry.add_or_update_consumer(consumer)
            self.client.request_counter.add_request(
                consumer=consumer_identifier,
                method=request.method,
                path=path_template,
                status_code=response_status,
                response_time=response_time,
                request_size=request.headers.get("Content-Length"),
                response_size=response_headers.get("Content-Length"),
            )
            if response_status == 422 and response_body and response_headers.get("Content-Type") == "application/json":
                with contextlib.suppress(json.JSONDecodeError):
                    body = json.loads(response_body)
                    if isinstance(body, dict) and "detail" in body and isinstance(body["detail"], list):
                        # Log FastAPI / Pydantic validation errors
                        self.client.validation_error_counter.add_validation_errors(
                            consumer=consumer_identifier,
                            method=request.method,
                            path=path_template,
                            detail=body["detail"],
                        )
            if response_status == 500 and exception is not None:
                self.client.server_error_counter.add_server_error(
                    consumer=consumer_identifier,
                    method=request.method,
                    path=path_template,
                    exception=exception,
                )

    @staticmethod
    def get_path_template(request: Request) -> Tuple[str, bool]:
        for route in request.app.routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                return route.path, True
        return request.url.path, False

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


def _register_shutdown_handler(app: Union[ASGIApp, Router], shutdown_handler: Callable[[], Any]) -> None:
    if isinstance(app, Router):
        app.add_event_handler("shutdown", shutdown_handler)
    elif hasattr(app, "app"):
        _register_shutdown_handler(app.app, shutdown_handler)
