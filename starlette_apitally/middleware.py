from __future__ import annotations

import re
import sys
import time
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple
from uuid import UUID

import starlette
from httpx import HTTPStatusError
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Match
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR
from starlette.testclient import TestClient
from starlette.types import ASGIApp

import starlette_apitally
from starlette_apitally.metrics import Metrics


if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response


class ApitallyMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        client_id: str,
        env: str = "default",
        app_version: Optional[str] = None,
        send_every: float = 60,
        filter_unhandled_paths: bool = True,
        openapi_url: Optional[str] = "/openapi.json",
    ) -> None:
        try:
            UUID(client_id)
        except ValueError:
            raise ValueError(f"invalid client_id '{client_id}' (expected hexadecimal UUID format)")
        if re.match(r"^[a-z\-]{1,32}$", env) is None:
            raise ValueError(f"invalid env '{env}' (expected 1-32 alphanumeric lowercase characters and hyphens only)")
        if app_version is not None and len(app_version) > 32:
            raise ValueError(f"invalid app_version '{app_version}' (expected 1-32 characters)")
        if send_every < 10:
            raise ValueError("send_every has to be greater or equal to 10 seconds")

        self.app_version = app_version
        self.filter_unhandled_paths = filter_unhandled_paths
        self.openapi_url = openapi_url
        self.metrics = Metrics(client_id=client_id, env=env, send_every=send_every)
        self.metrics.send_app_info(
            versions=self.get_versions(),
            openapi=self.get_openapi(app),
        )
        super().__init__(app)

    def get_versions(self) -> Dict[str, Any]:
        return {
            "app_version": self.app_version,
            "client_version": starlette_apitally.__version__,
            "starlette_version": starlette.__version__,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        }

    def get_openapi(self, app: ASGIApp) -> Optional[Dict[str, Any]]:
        if not self.openapi_url:
            return None
        try:
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get(self.openapi_url)
            response.raise_for_status()
            return response.json()
        except HTTPStatusError:
            return None

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            start_time = time.perf_counter()
            response = await call_next(request)
        except BaseException as e:
            await self.log_request(
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

    async def log_request(self, request: Request, status_code: int, response_time: float) -> None:
        path_template, is_handled_path = self.get_path_template(request)
        if is_handled_path or not self.filter_unhandled_paths:
            await self.metrics.log_request(
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
