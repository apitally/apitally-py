from __future__ import annotations

import sys
import time
from threading import Timer
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple

import flask
from werkzeug.exceptions import NotFound
from werkzeug.test import Client

import apitally
from apitally.client.threading import ApitallyClient


if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment
    from werkzeug.routing.map import Map


class ApitallyMiddleware:
    def __init__(
        self,
        app: WSGIApplication,
        client_id: str,
        env: str = "default",
        app_version: Optional[str] = None,
        enable_keys: bool = False,
        sync_interval: float = 60,
        openapi_url: Optional[str] = "/openapi.json",
        url_map: Optional[Map] = None,
        filter_unhandled_paths: bool = True,
    ) -> None:
        url_map = url_map or _get_url_map(app)
        if url_map is None:
            raise ValueError(
                "Could not extract url_map from app. Please provide it as an argument to ApitallyMiddleware."
            )
        self.app = app
        self.url_map = url_map
        self.filter_unhandled_paths = filter_unhandled_paths
        self.client = ApitallyClient(client_id=client_id, env=env, enable_keys=enable_keys, sync_interval=sync_interval)
        self.client.start_sync_loop()

        # Get and send app info after a short delay to allow app routes to be registered first
        timer = Timer(0.5, self.delayed_send_app_info, kwargs={"app_version": app_version, "openapi_url": openapi_url})
        timer.start()

    def delayed_send_app_info(self, app_version: Optional[str] = None, openapi_url: Optional[str] = None) -> None:
        app_info = _get_app_info(self.app, self.url_map, app_version, openapi_url)
        self.client.send_app_info(app_info=app_info)

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        status_code = 200

        def catching_start_response(status: str, headers, exc_info=None):
            nonlocal status_code
            status_code = int(status.split(" ")[0])
            return start_response(status, headers, exc_info)

        start_time = time.perf_counter()
        response = self.app(environ, catching_start_response)
        self.log_request(
            environ=environ,
            status_code=status_code,
            response_time=time.perf_counter() - start_time,
        )
        return response

    def log_request(self, environ: WSGIEnvironment, status_code: int, response_time: float) -> None:
        path_template, is_handled_path = self.get_path_template(environ)
        if is_handled_path or not self.filter_unhandled_paths:
            self.client.request_logger.log_request(
                method=environ["REQUEST_METHOD"],
                path=path_template,
                status_code=status_code,
                response_time=response_time,
            )

    def get_path_template(self, environ: WSGIEnvironment) -> Tuple[str, bool]:
        url_adapter = self.url_map.bind_to_environ(environ)
        try:
            endpoint, _ = url_adapter.match()
            rule = self.url_map._rules_by_endpoint[endpoint][0]
            return rule.rule, True
        except NotFound:
            return environ["PATH_INFO"], False


def _get_app_info(
    app: WSGIApplication,
    url_map: Map,
    app_version: Optional[str],
    openapi_url: Optional[str],
) -> Dict[str, Any]:
    app_info: Dict[str, Any] = {}
    if openapi := _get_openapi(app, openapi_url):
        app_info["openapi"] = openapi
    elif endpoints := _get_endpoint_info(url_map):
        app_info["paths"] = endpoints
    app_info["versions"] = _get_versions(app_version)
    app_info["client"] = "apitally-python"
    return app_info


def _get_url_map(app: WSGIApplication) -> Optional[Map]:
    if hasattr(app, "url_map"):
        return app.url_map
    elif hasattr(app, "__self__") and hasattr(app.__self__, "url_map"):
        return app.__self__.url_map
    return None


def _get_endpoint_info(url_map: Map) -> List[Dict[str, str]]:
    return [
        {"path": rule.rule, "method": method}
        for rule in url_map.iter_rules()
        if rule.methods is not None and rule.rule != "/static/<path:filename>"
        for method in rule.methods
        if method not in ["HEAD", "OPTIONS"]
    ]


def _get_openapi(app: WSGIApplication, openapi_url: Optional[str]) -> Optional[str]:
    if not openapi_url:
        return None
    client = Client(app)
    response = client.get(openapi_url)
    if response.status_code != 200:
        return None
    return response.get_data(as_text=True)


def _get_versions(app_version: Optional[str]) -> Dict[str, str]:
    versions = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "apitally": apitally.__version__,
        "flask": flask.__version__,
    }
    if app_version:
        versions["app"] = app_version
    return versions
