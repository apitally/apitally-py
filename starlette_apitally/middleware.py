from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Optional, Tuple
from uuid import UUID

from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Match
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR
from starlette.types import ASGIApp

from starlette_apitally.client import ApitallyClient


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
        self.client.send_app_info(app=app, app_version=app_version, openapi_url=openapi_url)
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
