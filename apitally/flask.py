from __future__ import annotations

import sys
import time
from functools import wraps
from importlib.metadata import version
from threading import Timer
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple, Type

from flask import Flask, g, make_response, request
from werkzeug.exceptions import NotFound
from werkzeug.test import Client

from apitally.client.base import ApitallyKeyCacheBase, KeyInfo
from apitally.client.threading import ApitallyClient


if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIApplication, WSGIEnvironment
    from werkzeug.routing.map import Map


__all__ = ["ApitallyMiddleware", "require_api_key"]


class ApitallyMiddleware:
    def __init__(
        self,
        app: Flask,
        client_id: str,
        env: str = "default",
        app_version: Optional[str] = None,
        sync_api_keys: bool = False,
        openapi_url: Optional[str] = None,
        filter_unhandled_paths: bool = True,
        key_cache_class: Optional[Type[ApitallyKeyCacheBase]] = None,
    ) -> None:
        self.app = app
        self.wsgi_app = app.wsgi_app
        self.filter_unhandled_paths = filter_unhandled_paths
        self.client = ApitallyClient(
            client_id=client_id,
            env=env,
            sync_api_keys=sync_api_keys,
            key_cache_class=key_cache_class,
        )
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

        def catching_start_response(status: str, headers, exc_info=None):
            nonlocal status_code
            status_code = int(status.split(" ")[0])
            return start_response(status, headers, exc_info)

        start_time = time.perf_counter()
        with self.app.app_context():
            response = self.wsgi_app(environ, catching_start_response)
            self.log_request(
                environ=environ,
                status_code=status_code,
                response_time=time.perf_counter() - start_time,
            )
        return response

    def log_request(self, environ: WSGIEnvironment, status_code: int, response_time: float) -> None:
        rule, is_handled_path = self.get_rule(environ)
        if is_handled_path or not self.filter_unhandled_paths:
            self.client.request_logger.log_request(
                consumer=self.get_consumer(),
                method=environ["REQUEST_METHOD"],
                path=rule,
                status_code=status_code,
                response_time=response_time,
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
        if "consumer_identifier" in g:
            return str(g.consumer_identifier)
        if "key_info" in g and isinstance(g.key_info, KeyInfo):
            return f"key:{g.key_info.key_id}"
        return None


def require_api_key(func=None, *, scopes: Optional[List[str]] = None, custom_header: Optional[str] = None):
    def decorator(func):
        @wraps(func)
        def wrapped_func(*args, **kwargs):
            api_key: Optional[str]
            if custom_header is None:
                authorization = request.headers.get("Authorization")
                if authorization is None:
                    return make_response("Not authenticated", 401, {"WWW-Authenticate": "ApiKey"})
                scheme, _, api_key = authorization.partition(" ")
                if scheme.lower() != "apikey":
                    return make_response("Unsupported authentication scheme", 401, {"WWW-Authenticate": "ApiKey"})
            else:
                api_key = request.headers.get(custom_header)
                if api_key is None:
                    return make_response("Missing API key", 403)
            key_info = ApitallyClient.get_instance().key_registry.get(api_key)
            if key_info is None:
                return make_response("Invalid API key", 403)
            if scopes is not None and not key_info.has_scopes(scopes):
                return make_response("Permission denied", 403)
            g.key_info = key_info
            return func(*args, **kwargs)

        return wrapped_func

    return decorator if func is None else decorator(func)


def _get_app_info(app: Flask, app_version: Optional[str] = None, openapi_url: Optional[str] = None) -> Dict[str, Any]:
    app_info: Dict[str, Any] = {}
    if openapi_url and (openapi := _get_openapi(app, openapi_url)):
        app_info["openapi"] = openapi
    if paths := _get_paths(app.url_map):
        app_info["paths"] = paths
    app_info["versions"] = _get_versions(app_version)
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


def _get_versions(app_version: Optional[str]) -> Dict[str, str]:
    versions = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "apitally": version("apitally"),
        "flask": version("flask"),
    }
    if app_version:
        versions["app"] = app_version
    return versions
