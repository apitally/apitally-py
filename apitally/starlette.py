from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union
from warnings import warn

from httpx import HTTPStatusError
from starlette.concurrency import iterate_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import BaseRoute, Match, Router
from starlette.schemas import EndpointInfo, SchemaGenerator
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR
from starlette.testclient import TestClient
from starlette.types import ASGIApp

from apitally.client.asyncio import ApitallyClient
from apitally.client.base import Consumer as ApitallyConsumer
from apitally.common import get_versions


if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response


__all__ = ["ApitallyMiddleware", "ApitallyConsumer"]


class ApitallyMiddleware(BaseHTTPMiddleware):
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
        self.filter_unhandled_paths = filter_unhandled_paths
        self.identify_consumer_callback = identify_consumer_callback
        self.client = ApitallyClient(client_id=client_id, env=env)
        self.client.start_sync_loop()
        self._delayed_set_startup_data_task: Optional[asyncio.Task] = None
        self.delayed_set_startup_data(app_version, openapi_url)
        _register_shutdown_handler(app, self.client.handle_shutdown)
        super().__init__(app)

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

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            start_time = time.perf_counter()
            response = await call_next(request)
        except BaseException as e:
            await self.add_request(
                request=request,
                response=None,
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                response_time=time.perf_counter() - start_time,
                exception=e,
            )
            raise e from None
        else:
            await self.add_request(
                request=request,
                response=response,
                status_code=response.status_code,
                response_time=time.perf_counter() - start_time,
            )
        return response

    async def add_request(
        self,
        request: Request,
        response: Optional[Response],
        status_code: int,
        response_time: float,
        exception: Optional[BaseException] = None,
    ) -> None:
        path_template, is_handled_path = self.get_path_template(request)
        if (is_handled_path or not self.filter_unhandled_paths) and request.method != "OPTIONS":
            consumer = self.get_consumer(request)
            consumer_identifier = consumer.identifier if consumer else None
            self.client.consumer_registry.add_or_update_consumer(consumer)
            self.client.request_counter.add_request(
                consumer=consumer_identifier,
                method=request.method,
                path=path_template,
                status_code=status_code,
                response_time=response_time,
                request_size=request.headers.get("Content-Length"),
                response_size=response.headers.get("Content-Length") if response is not None else None,
            )
            if (
                status_code == 422
                and response is not None
                and response.headers.get("Content-Type") == "application/json"
            ):
                body = await self.get_response_json(response)
                if isinstance(body, dict) and "detail" in body and isinstance(body["detail"], list):
                    # Log FastAPI / Pydantic validation errors
                    self.client.validation_error_counter.add_validation_errors(
                        consumer=consumer_identifier,
                        method=request.method,
                        path=path_template,
                        detail=body["detail"],
                    )
            if status_code == 500 and exception is not None:
                self.client.server_error_counter.add_server_error(
                    consumer=consumer_identifier,
                    method=request.method,
                    path=path_template,
                    exception=exception,
                )

    @staticmethod
    async def get_response_json(response: Response) -> Any:
        if hasattr(response, "body"):
            try:
                return json.loads(response.body)
            except json.JSONDecodeError:  # pragma: no cover
                return None
        elif hasattr(response, "body_iterator"):
            try:
                response_body = [section async for section in response.body_iterator]
                response.body_iterator = iterate_in_threadpool(iter(response_body))
                return json.loads(b"".join(response_body))
            except json.JSONDecodeError:  # pragma: no cover
                return None

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
