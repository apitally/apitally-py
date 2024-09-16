from __future__ import annotations

import time
from threading import Timer
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple
from warnings import warn

from flask import Flask, g
from flask.wrappers import Response
from werkzeug.datastructures import Headers
from werkzeug.exceptions import NotFound
from werkzeug.test import Client

from apitally.client.base import Consumer as ApitallyConsumer
from apitally.client.threading import ApitallyClient
from apitally.common import get_versions


if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment
    from werkzeug.routing.map import Map


__all__ = ["ApitallyMiddleware", "ApitallyConsumer"]


class ApitallyMiddleware:
    def __init__(
        self,
        app: Flask,
        client_id: str,
        env: str = "dev",
        app_version: Optional[str] = None,
        openapi_url: Optional[str] = None,
        filter_unhandled_paths: bool = True,
    ) -> None:
        self.app = app
        self.wsgi_app = app.wsgi_app
        self.filter_unhandled_paths = filter_unhandled_paths
        self.patch_handle_exception()
        self.client = ApitallyClient(client_id=client_id, env=env)
        self.client.start_sync_loop()
        self.delayed_set_startup_data(app_version, openapi_url)

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
        status_code = 200
        response_headers = Headers([])

        def catching_start_response(status: str, headers: List[Tuple[str, str]], exc_info=None):
            nonlocal status_code, response_headers
            status_code = int(status.split(" ")[0])
            response_headers = Headers(headers)
            return start_response(status, headers, exc_info)

        start_time = time.perf_counter()
        with self.app.app_context():
            response = self.wsgi_app(environ, catching_start_response)
            self.add_request(
                environ=environ,
                status_code=status_code,
                response_time=time.perf_counter() - start_time,
                response_headers=response_headers,
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
        environ: WSGIEnvironment,
        status_code: int,
        response_time: float,
        response_headers: Headers,
    ) -> None:
        rule, is_handled_path = self.get_rule(environ)
        if (is_handled_path or not self.filter_unhandled_paths) and environ["REQUEST_METHOD"] != "OPTIONS":
            consumer = self.get_consumer()
            consumer_identifier = consumer.identifier if consumer else None
            self.client.consumer_registry.add_or_update_consumer(consumer)
            self.client.request_counter.add_request(
                consumer=consumer_identifier,
                method=environ["REQUEST_METHOD"],
                path=rule,
                status_code=status_code,
                response_time=response_time,
                request_size=environ.get("CONTENT_LENGTH"),
                response_size=response_headers.get("Content-Length", type=int),
            )
            if status_code == 500 and "unhandled_exception" in g:
                self.client.server_error_counter.add_server_error(
                    consumer=consumer_identifier,
                    method=environ["REQUEST_METHOD"],
                    path=rule,
                    exception=g.unhandled_exception,
                )

    def get_rule(self, environ: WSGIEnvironment) -> Tuple[str, bool]:
        url_adapter = self.app.url_map.bind_to_environ(environ)
        try:
            endpoint, _ = url_adapter.match()
            rule = self.app.url_map._rules_by_endpoint[endpoint][0]
            return rule.rule, True
        except NotFound:
            return environ["PATH_INFO"], False

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
