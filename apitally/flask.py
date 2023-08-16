from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple

import flask
from werkzeug.exceptions import NotFound
from werkzeug.test import Client

import apitally
from apitally.client.threading import ApitallyClient
from apitally.client.utils import validate_client_params


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
        filter_unhandled_paths: bool = True,
        openapi_url: Optional[str] = "/openapi.json",
        url_map: Optional[Map] = None,
    ) -> None:
        self.app = app
        self.url_map = url_map or self.get_url_map()
        if self.url_map is None:
            raise ValueError(
                "Could not extract url_map from app. Please provide it as an argument to ApitallyMiddleware."
            )
        self.filter_unhandled_paths = filter_unhandled_paths
        validate_client_params(client_id=client_id, env=env, app_version=app_version, sync_interval=sync_interval)
        self.client = ApitallyClient(client_id=client_id, env=env, enable_keys=enable_keys, sync_interval=sync_interval)
        self.client.send_app_info(app_info=_get_app_info(self.app, self.url_map, app_version, openapi_url))
        self.client.start_sync_loop()

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

    def get_url_map(self) -> Optional[Map]:
        if hasattr(self.app, "url_map"):
            return self.app.url_map
        elif hasattr(self.app, "__self__") and hasattr(self.app.__self__, "url_map"):
            return self.app.__self__.url_map
        return None

    def get_path_template(self, environ: WSGIEnvironment) -> Tuple[str, bool]:
        if self.url_map is None:
            return environ["PATH_INFO"], False  # pragma: no cover
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
