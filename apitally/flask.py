from __future__ import annotations

import time
from threading import Timer
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple

from flask import Flask, g
from werkzeug.datastructures import Headers
from werkzeug.exceptions import NotFound
from werkzeug.test import Client

from apitally.client.threading import ApitallyClient
from apitally.common import get_versions


if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment
    from werkzeug.routing.map import Map


__all__ = ["ApitallyMiddleware"]


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
        self.client = ApitallyClient(client_id=client_id, env=env)
        self.client.start_sync_loop()
        self.delayed_set_app_info(app_version, openapi_url)

    def delayed_set_app_info(self, app_version: Optional[str] = None, openapi_url: Optional[str] = None) -> None:
        # Short delay to allow app routes to be registered first
        timer = Timer(1.0, self._delayed_set_app_info, kwargs={"app_version": app_version, "openapi_url": openapi_url})
        timer.start()

    def _delayed_set_app_info(self, app_version: Optional[str] = None, openapi_url: Optional[str] = None) -> None:
        app_info = _get_app_info(self.app, app_version, openapi_url)
        self.client.set_app_info(app_info)

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

    def add_request(
        self, environ: WSGIEnvironment, status_code: int, response_time: float, response_headers: Headers
    ) -> None:
        rule, is_handled_path = self.get_rule(environ)
        if is_handled_path or not self.filter_unhandled_paths:
            self.client.request_counter.add_request(
                consumer=self.get_consumer(),
                method=environ["REQUEST_METHOD"],
                path=rule,
                status_code=status_code,
                response_time=response_time,
                request_size=environ.get("CONTENT_LENGTH"),
                response_size=response_headers.get("Content-Length", type=int),
            )

    def get_rule(self, environ: WSGIEnvironment) -> Tuple[str, bool]:
        url_adapter = self.app.url_map.bind_to_environ(environ)
        try:
            endpoint, _ = url_adapter.match()
            rule = self.app.url_map._rules_by_endpoint[endpoint][0]
            return rule.rule, True
        except NotFound:
            return environ["PATH_INFO"], False

    def get_consumer(self) -> Optional[str]:
        return str(g.consumer_identifier) if "consumer_identifier" in g else None


def _get_app_info(app: Flask, app_version: Optional[str] = None, openapi_url: Optional[str] = None) -> Dict[str, Any]:
    app_info: Dict[str, Any] = {}
    if openapi_url and (openapi := _get_openapi(app, openapi_url)):
        app_info["openapi"] = openapi
    if paths := _get_paths(app.url_map):
        app_info["paths"] = paths
    app_info["versions"] = get_versions("flask", app_version=app_version)
    app_info["client"] = "python:flask"
    return app_info


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
