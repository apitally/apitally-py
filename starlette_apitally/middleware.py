import time
from typing import Optional, Tuple

from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR
from starlette.types import ASGIApp

from starlette_apitally.metrics import Metrics


class ApitallyMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        client_id: str,
        app_version: Optional[str] = None,
        send_every: int = 10,
        filter_unhandled_paths: bool = True,
    ) -> None:
        self.filter_unhandled_paths = filter_unhandled_paths
        self.metrics = Metrics(client_id=client_id, app_version=app_version, send_every=send_every)
        self.metrics.send_versions()
        super().__init__(app)

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
