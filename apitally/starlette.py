from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import starlette
from httpx import HTTPStatusError
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    BaseUser,
)
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import HTTPConnection
from starlette.routing import BaseRoute, Match, Router
from starlette.schemas import EndpointInfo, SchemaGenerator
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR
from starlette.testclient import TestClient
from starlette.types import ASGIApp

import apitally
from apitally.client.asyncio import ApitallyClient
from apitally.client.base import KeyInfo


if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response


__all__ = ["ApitallyMiddleware", "ApitallyKeysBackend"]


class ApitallyMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        client_id: str,
        env: str = "default",
        app_version: Optional[str] = None,
        enable_keys: bool = False,
        sync_interval: float = 60,
        openapi_url: Optional[str] = "/openapi.json",
        filter_unhandled_paths: bool = True,
    ) -> None:
        self.filter_unhandled_paths = filter_unhandled_paths
        self.client = ApitallyClient(client_id=client_id, env=env, enable_keys=enable_keys, sync_interval=sync_interval)
        self.client.send_app_info(app_info=_get_app_info(app, app_version, openapi_url))
        self.client.start_sync_loop()
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            start_time = time.perf_counter()
            response = await call_next(request)
        except BaseException as e:
            self.log_request(
                request=request,
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                response_time=time.perf_counter() - start_time,
            )
            raise e from None
        else:
            response.background = BackgroundTask(
                self.log_request,
                request=request,
                status_code=response.status_code,
                response_time=time.perf_counter() - start_time,
            )
        return response

    def log_request(self, request: Request, status_code: int, response_time: float) -> None:
        path_template, is_handled_path = self.get_path_template(request)
        if is_handled_path or not self.filter_unhandled_paths:
            self.client.request_logger.log_request(
                method=request.method,
                path=path_template,
                status_code=status_code,
                response_time=response_time,
            )

    @staticmethod
    def get_path_template(request: Request) -> Tuple[str, bool]:
        for route in request.app.routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                return route.path, True
        return request.url.path, False


class ApitallyKeysBackend(AuthenticationBackend):
    async def authenticate(self, conn: HTTPConnection) -> Optional[Tuple[AuthCredentials, BaseUser]]:
        if "Authorization" not in conn.headers:
            return None
        auth = conn.headers["Authorization"]
        scheme, _, param = auth.partition(" ")
        if scheme.lower() != "apikey":
            return None
        key_info = ApitallyClient.get_instance().key_registry.get(param)
        if key_info is None:
            raise AuthenticationError("Invalid API key")
        return AuthCredentials(["authenticated"] + key_info.scopes), ApitallyKeyUser(key_info)


class ApitallyKeyUser(BaseUser):
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


def _get_app_info(app: ASGIApp, app_version: Optional[str], openapi_url: Optional[str]) -> Dict[str, Any]:
    app_info: Dict[str, Any] = {}
    if openapi_url and (openapi := _get_openapi(app, openapi_url)):
        app_info["openapi"] = openapi
    elif endpoints := _get_endpoint_info(app):
        app_info["paths"] = [{"path": endpoint.path, "method": endpoint.http_method} for endpoint in endpoints]
    app_info["versions"] = _get_versions(app_version)
    app_info["client"] = "apitally-python"
    app_info["framework"] = "starlette"
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


def _get_routes(app: ASGIApp) -> List[BaseRoute]:
    if isinstance(app, Router):
        return app.routes
    elif hasattr(app, "app"):
        return _get_routes(app.app)
    return []


def _get_versions(app_version: Optional[str]) -> Dict[str, str]:
    versions = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "apitally": apitally.__version__,
        "starlette": starlette.__version__,
    }
    try:
        import fastapi

        versions["fastapi"] = fastapi.__version__
    except (ImportError, AttributeError):  # pragma: no cover
        pass
    if app_version:
        versions["app"] = app_version
    return versions
