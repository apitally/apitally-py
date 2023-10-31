from __future__ import annotations

import asyncio
import json
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    Union,
)

from httpx import HTTPStatusError
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    BaseUser,
)
from starlette.concurrency import iterate_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import HTTPConnection
from starlette.routing import BaseRoute, Match, Router
from starlette.schemas import EndpointInfo, SchemaGenerator
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR
from starlette.testclient import TestClient
from starlette.types import ASGIApp

from apitally.client.asyncio import ApitallyClient
from apitally.client.base import ApitallyKeyCacheBase, KeyInfo


if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response


__all__ = ["ApitallyMiddleware", "APIKeyAuth"]


class ApitallyMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        client_id: str,
        env: str = "default",
        app_version: Optional[str] = None,
        sync_api_keys: bool = False,
        openapi_url: Optional[str] = "/openapi.json",
        filter_unhandled_paths: bool = True,
        identify_consumer_callback: Optional[Callable[[Request], Optional[str]]] = None,
        key_cache_class: Optional[Type[ApitallyKeyCacheBase]] = None,
    ) -> None:
        self.filter_unhandled_paths = filter_unhandled_paths
        self.identify_consumer_callback = identify_consumer_callback
        self.client = ApitallyClient(
            client_id=client_id,
            env=env,
            sync_api_keys=sync_api_keys,
            key_cache_class=key_cache_class,
        )
        self.client.start_sync_loop()
        self.delayed_set_app_info(app_version, openapi_url)
        _register_shutdown_handler(app, self.client.handle_shutdown)
        super().__init__(app)

    def delayed_set_app_info(self, app_version: Optional[str] = None, openapi_url: Optional[str] = None) -> None:
        asyncio.create_task(self._delayed_set_app_info(app_version, openapi_url))

    async def _delayed_set_app_info(self, app_version: Optional[str] = None, openapi_url: Optional[str] = None) -> None:
        await asyncio.sleep(1.0)  # Short delay to allow app routes to be registered first
        app_info = _get_app_info(self.app, app_version, openapi_url)
        self.client.set_app_info(app_info)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            start_time = time.perf_counter()
            response = await call_next(request)
        except BaseException as e:
            await self.log_request(
                request=request,
                response=None,
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                response_time=time.perf_counter() - start_time,
            )
            raise e from None
        else:
            await self.log_request(
                request=request,
                response=response,
                status_code=response.status_code,
                response_time=time.perf_counter() - start_time,
            )
        return response

    async def log_request(
        self, request: Request, response: Optional[Response], status_code: int, response_time: float
    ) -> None:
        path_template, is_handled_path = self.get_path_template(request)
        if is_handled_path or not self.filter_unhandled_paths:
            consumer = self.get_consumer(request)
            self.client.request_logger.log_request(
                consumer=consumer,
                method=request.method,
                path=path_template,
                status_code=status_code,
                response_time=response_time,
            )
            if (
                status_code == 422
                and response is not None
                and response.headers.get("Content-Type") == "application/json"
            ):
                body = await self.get_response_json(response)
                if isinstance(body, dict) and "detail" in body and isinstance(body["detail"], list):
                    # Log FastAPI / Pydantic validation errors
                    self.client.validation_error_logger.log_validation_errors(
                        consumer=consumer,
                        method=request.method,
                        path=path_template,
                        detail=body["detail"],
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

    def get_consumer(self, request: Request) -> Optional[str]:
        if hasattr(request.state, "consumer_identifier"):
            return str(request.state.consumer_identifier)
        if self.identify_consumer_callback is not None:
            consumer_identifier = self.identify_consumer_callback(request)
            if consumer_identifier is not None:
                return str(consumer_identifier)
        if hasattr(request.state, "key_info") and isinstance(key_info := request.state.key_info, KeyInfo):
            return f"key:{key_info.key_id}"
        if "user" in request.scope and isinstance(user := request.scope["user"], APIKeyUser):
            return f"key:{user.key_info.key_id}"
        return None


class APIKeyAuth(AuthenticationBackend):
    def __init__(self, custom_header: Optional[str] = None) -> None:
        self.custom_header = custom_header

    async def authenticate(self, conn: HTTPConnection) -> Optional[Tuple[AuthCredentials, BaseUser]]:
        if self.custom_header is None:
            if "Authorization" not in conn.headers:
                return None
            auth = conn.headers["Authorization"]
            scheme, _, api_key = auth.partition(" ")
            if scheme.lower() != "apikey":
                return None
        elif self.custom_header not in conn.headers:
            return None
        else:
            api_key = conn.headers[self.custom_header]
        key_info = ApitallyClient.get_instance().key_registry.get(api_key)
        if key_info is None:
            raise AuthenticationError("Invalid API key")
        return AuthCredentials(["authenticated"] + key_info.scopes), APIKeyUser(key_info)


class APIKeyUser(BaseUser):
    def __init__(self, key_info: KeyInfo) -> None:
        self.key_info = key_info

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return self.key_info.name

    @property
    def identity(self) -> str:
        return str(self.key_info.key_id)


def _get_app_info(app: ASGIApp, app_version: Optional[str] = None, openapi_url: Optional[str] = None) -> Dict[str, Any]:
    app_info: Dict[str, Any] = {}
    if openapi_url and (openapi := _get_openapi(app, openapi_url)):
        app_info["openapi"] = openapi
    if endpoints := _get_endpoint_info(app):
        app_info["paths"] = [{"path": endpoint.path, "method": endpoint.http_method} for endpoint in endpoints]
    app_info["versions"] = _get_versions(app_version)
    app_info["client"] = "python:starlette"
    return app_info


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


def _get_versions(app_version: Optional[str]) -> Dict[str, str]:
    versions = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "apitally": version("apitally"),
        "starlette": version("starlette"),
    }
    try:
        versions["fastapi"] = version("fastapi")
    except PackageNotFoundError:  # pragma: no cover
        pass
    if app_version:
        versions["app"] = app_version
    return versions
