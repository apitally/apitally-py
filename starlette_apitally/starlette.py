from __future__ import annotations

import re
import sys
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from uuid import UUID

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

import starlette_apitally
from starlette_apitally.client import ApitallyClient
from starlette_apitally.keys import Key


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
        send_every: float = 60,
        filter_unhandled_paths: bool = True,
        openapi_url: Optional[str] = "/openapi.json",
    ) -> None:
        try:
            UUID(client_id)
        except ValueError:
            raise ValueError(f"invalid client_id '{client_id}' (expected hexadecimal UUID format)")
        if re.match(r"^[\w-]{1,32}$", env) is None:
            raise ValueError(f"invalid env '{env}' (expected 1-32 alphanumeric lowercase characters and hyphens only)")
        if app_version is not None and len(app_version) > 32:
            raise ValueError(f"invalid app_version '{app_version}' (expected 1-32 characters)")
        if send_every < 10:
            raise ValueError("send_every has to be greater or equal to 10 seconds")

        self.filter_unhandled_paths = filter_unhandled_paths
        self.client = ApitallyClient(client_id=client_id, env=env, enable_keys=enable_keys, send_every=send_every)
        self.client.send_app_info(app_info=_get_app_info(app, app_version, openapi_url))
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
            self.client.requests.log_request(
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
        scheme, _, credentials = auth.partition(" ")
        if scheme.lower() != "apikey":
            return None
        key = ApitallyClient.get_instance().keys.get(credentials)
        if key is None:
            raise AuthenticationError("Invalid API key")
        return AuthCredentials(["authenticated"] + key.scopes), ApitallyKeyUser(key)


class ApitallyKeyUser(BaseUser):
    def __init__(self, key: Key) -> None:
        self.key = key

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return self.key.name

    @property
    def identity(self) -> str:
        return str(self.key.key_id)


def _get_app_info(app: ASGIApp, app_version: Optional[str], openapi_url: Optional[str]) -> Dict[str, Any]:
    app_info: Dict[str, Any] = {}
    if openapi := _get_openapi(app, openapi_url):
        app_info["openapi"] = openapi
    elif endpoints := _get_endpoint_info(app):
        app_info["paths"] = [{"path": endpoint.path, "method": endpoint.http_method} for endpoint in endpoints]
    app_info["versions"] = _get_versions(app_version)
    app_info["client"] = "starlette-apitally"
    return app_info


def _get_openapi(app: ASGIApp, openapi_url: Optional[str]) -> Optional[str]:
    if not openapi_url:
        return None
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
        "starlette-apitally": starlette_apitally.__version__,
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
