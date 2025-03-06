from __future__ import annotations

import time
from io import BytesIO
from threading import Timer
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple
from warnings import warn

from flask import Flask, g
from flask.wrappers import Request, Response
from werkzeug.datastructures import Headers
from werkzeug.exceptions import NotFound
from werkzeug.test import Client

from apitally.client.client_threading import ApitallyClient
from apitally.client.consumers import Consumer as ApitallyConsumer
from apitally.client.request_logging import (
    BODY_TOO_LARGE,
    MAX_BODY_SIZE,
    RequestLogger,
    RequestLoggingConfig,
)
from apitally.common import get_versions


if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment
    from werkzeug.routing.map import Map


__all__ = ["ApitallyMiddleware", "ApitallyConsumer", "RequestLoggingConfig"]


class ApitallyMiddleware:
    def __init__(
        self,
        app: Flask,
        client_id: str,
        env: str = "dev",
        request_logging_config: Optional[RequestLoggingConfig] = None,
        app_version: Optional[str] = None,
        openapi_url: Optional[str] = None,
        proxy: Optional[str] = None,
    ) -> None:
        self.app = app
        self.wsgi_app = app.wsgi_app
        self.patch_handle_exception()
        self.client = ApitallyClient(
            client_id=client_id,
            env=env,
            request_logging_config=request_logging_config,
            proxy=proxy,
        )
        self.client.start_sync_loop()
        self.delayed_set_startup_data(app_version, openapi_url)

        self.capture_request_body = (
            self.client.request_logger.config.enabled and self.client.request_logger.config.log_request_body
        )
        self.capture_response_body = (
            self.client.request_logger.config.enabled and self.client.request_logger.config.log_response_body
        )

    def delayed_set_startup_data(self, app_version: Optional[str] = None, openapi_url: Optional[str] = None) -> None:
        # Short delay to allow app routes to be registered first
        timer = Timer(
            1.0,
            self._delayed_set_startup_data,
            kwargs={"app_version": app_version, "openapi_url": openapi_url},
        )
        timer.start()

    def _delayed_set_startup_data(self, app_version: Optional[str] = None, openapi_url: Optional[str] = None) -> None:
        data = _get_startup_data(self.app, app_version, openapi_url)
        self.client.set_startup_data(data)

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        if not self.client.enabled:
            return self.wsgi_app(environ, start_response)

        timestamp = time.time()
        response_headers = Headers([])
        status_code = 0

        def catching_start_response(status: str, headers: List[Tuple[str, str]], exc_info=None):
            nonlocal status_code, response_headers
            status_code = int(status.split(" ")[0])
            response_headers = Headers(headers)
            return start_response(status, headers, exc_info)

        with self.app.app_context():
            request = Request(environ, populate_request=False, shallow=True)
            request_size = request.content_length
            request_body = b""
            if self.capture_request_body:
                request_body = (
                    _read_request_body(environ)
                    if request_size is not None and request_size <= MAX_BODY_SIZE
                    else BODY_TOO_LARGE
                )

            start_time = time.perf_counter()
            response = self.wsgi_app(environ, catching_start_response)
            response_time = time.perf_counter() - start_time

            response_body = b""
            response_content_type = response_headers.get("Content-Type")
            if self.capture_response_body and RequestLogger.is_supported_content_type(response_content_type):
                response_size = response_headers.get("Content-Length", type=int)
                if response_size is not None and response_size > MAX_BODY_SIZE:
                    response_body = BODY_TOO_LARGE
                else:
                    for chunk in response:
                        response_body += chunk
                        if len(response_body) > MAX_BODY_SIZE:
                            response_body = BODY_TOO_LARGE
                            break

            self.add_request(
                timestamp=timestamp,
                request=request,
                request_body=request_body,
                status_code=status_code,
                response_time=response_time,
                response_headers=response_headers,
                response_body=response_body,
            )
        return response

    def patch_handle_exception(self) -> None:
        original_handle_exception = self.app.handle_exception

        def handle_exception(e: Exception) -> Response:
            g.unhandled_exception = e
            return original_handle_exception(e)

        self.app.handle_exception = handle_exception  # type: ignore[method-assign]

    def add_request(
        self,
        timestamp: float,
        request: Request,
        request_body: bytes,
        status_code: int,
        response_time: float,
        response_headers: Headers,
        response_body: bytes,
    ) -> None:
        path = self.get_path(request.environ)
        response_size = response_headers.get("Content-Length", type=int)

        consumer = self.get_consumer()
        consumer_identifier = consumer.identifier if consumer else None
        self.client.consumer_registry.add_or_update_consumer(consumer)

        if path is not None and request.method != "OPTIONS":
            self.client.request_counter.add_request(
                consumer=consumer_identifier,
                method=request.method,
                path=path,
                status_code=status_code,
                response_time=response_time,
                request_size=request.content_length,
                response_size=response_size,
            )
            if status_code == 500 and "unhandled_exception" in g:
                self.client.server_error_counter.add_server_error(
                    consumer=consumer_identifier,
                    method=request.method,
                    path=path,
                    exception=g.unhandled_exception,
                )

        if self.client.request_logger.enabled:
            self.client.request_logger.log_request(
                request={
                    "timestamp": timestamp,
                    "method": request.method,
                    "path": path,
                    "url": request.url,
                    "headers": list(request.headers.items()),
                    "size": request.content_length,
                    "consumer": consumer_identifier,
                    "body": request_body,
                },
                response={
                    "status_code": status_code,
                    "response_time": response_time,
                    "headers": list(response_headers.items()),
                    "size": response_size,
                    "body": response_body,
                },
            )

    def get_path(self, environ: WSGIEnvironment) -> Optional[str]:
        url_adapter = self.app.url_map.bind_to_environ(environ)
        try:
            endpoint, _ = url_adapter.match()
            rule = self.app.url_map._rules_by_endpoint[endpoint][0]
            return rule.rule
        except NotFound:
            return None

    def get_consumer(self) -> Optional[ApitallyConsumer]:
        if "apitally_consumer" in g and g.apitally_consumer:
            return ApitallyConsumer.from_string_or_object(g.apitally_consumer)
        if "consumer_identifier" in g and g.consumer_identifier:
            # Keeping this for legacy support
            warn(
                "Providing a consumer identifier via `g.consumer_identifier` is deprecated, "
                "use `g.apitally_consumer` instead.",
                DeprecationWarning,
            )
            return ApitallyConsumer.from_string_or_object(g.consumer_identifier)
        return None


def _get_startup_data(
    app: Flask, app_version: Optional[str] = None, openapi_url: Optional[str] = None
) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if openapi_url and (openapi := _get_openapi(app, openapi_url)):
        data["openapi"] = openapi
    if paths := _get_paths(app.url_map):
        data["paths"] = paths
    data["versions"] = get_versions("flask", app_version=app_version)
    data["client"] = "python:flask"
    return data


def _get_paths(url_map: Map) -> List[Dict[str, str]]:
    return [
        {"path": rule.rule, "method": method}
        for rule in url_map.iter_rules()
        if rule.methods is not None and rule.rule != "/static/<path:filename>"
        for method in rule.methods
        if method not in ["HEAD", "OPTIONS"]
    ]


def _get_openapi(app: WSGIApplication, openapi_url: str) -> Optional[str]:
    client = Client(app)
    response = client.get(openapi_url)
    if response.status_code != 200:
        return None
    return response.get_data(as_text=True)


def _read_request_body(environ: WSGIEnvironment) -> bytes:
    length = int(environ.get("CONTENT_LENGTH", "0"))
    body = environ["wsgi.input"].read(length)
    environ["wsgi.input"] = BytesIO(body)
    return body
