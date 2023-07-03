import time
from typing import Optional, Tuple

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR
from starlette.types import ASGIApp

from starlette_apitally.sender import BufferedSender


class ApitallyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        self.sender = BufferedSender()
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            start_time = time.perf_counter()
            response = await call_next(request)
        except BaseException as e:
            self.enqueue(
                request,
                processing_time=time.perf_counter() - start_time,
            )
            raise e from None
        else:
            self.enqueue(
                request,
                processing_time=time.perf_counter() - start_time,
                response=response,
            )
        return response

    def enqueue(self, request: Request, processing_time: float, response: Optional[Response] = None) -> None:
        path_template, is_handled_path = self.get_path_template(request)
        status_code = response.status_code if response is not None else HTTP_500_INTERNAL_SERVER_ERROR
        self.sender.add(
            {
                "method": request.method,
                "path": path_template,
                "handled": is_handled_path,
                "status_code": status_code,
                "processing_time": processing_time,
            }
        )

    @staticmethod
    def get_path_template(request: Request) -> Tuple[str, bool]:
        for route in request.app.routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                return route.path, True
        return request.url.path, False
