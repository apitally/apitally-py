from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Type

from django.conf import settings
from django.core.exceptions import ViewDoesNotExist
from django.test import RequestFactory
from django.urls import URLPattern, URLResolver, get_resolver, resolve
from django.utils.module_loading import import_string

from apitally.client.base import ApitallyKeyCacheBase, KeyInfo
from apitally.client.threading import ApitallyClient


if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse


__all__ = ["ApitallyMiddleware"]


@dataclass
class ApitallyMiddlewareConfig:
    client_id: str
    env: str
    app_version: Optional[str]
    sync_api_keys: bool
    openapi_url: Optional[str]
    identify_consumer_callback: Optional[Callable[[HttpRequest], Optional[str]]]
    key_cache_class: Optional[Type[ApitallyKeyCacheBase]]


class ApitallyMiddleware:
    config: Optional[ApitallyMiddlewareConfig] = None

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        if self.config is None:
            config = getattr(settings, "APITALLY_MIDDLEWARE", {})
            self.configure(**config)
            assert self.config is not None
        self.views = _extract_views_from_url_patterns(get_resolver().url_patterns)
        self.client = ApitallyClient(
            client_id=self.config.client_id,
            env=self.config.env,
            sync_api_keys=self.config.sync_api_keys,
            key_cache_class=self.config.key_cache_class,
        )
        self.client.start_sync_loop()
        self.client.set_app_info(
            app_info=_get_app_info(
                views=self.views,
                app_version=self.config.app_version,
                openapi_url=self.config.openapi_url,
            )
        )

    @classmethod
    def configure(
        cls,
        client_id: str,
        env: str = "default",
        app_version: Optional[str] = None,
        sync_api_keys: bool = False,
        openapi_url: Optional[str] = None,
        identify_consumer_callback: Optional[str] = None,
        key_cache_class: Optional[Type[ApitallyKeyCacheBase]] = None,
    ) -> None:
        cls.config = ApitallyMiddlewareConfig(
            client_id=client_id,
            env=env,
            app_version=app_version,
            sync_api_keys=sync_api_keys,
            openapi_url=openapi_url,
            identify_consumer_callback=import_string(identify_consumer_callback)
            if identify_consumer_callback
            else None,
            key_cache_class=key_cache_class,
        )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        view = self.get_view(request)
        start_time = time.perf_counter()
        response = self.get_response(request)
        if request.method is not None and view is not None and view.is_api_view:
            consumer = self.get_consumer(request)
            self.client.request_logger.log_request(
                consumer=consumer,
                method=request.method,
                path=view.pattern,
                status_code=response.status_code,
                response_time=time.perf_counter() - start_time,
            )
            if (
                response.status_code == 422
                and (content_type := response.get("Content-Type")) is not None
                and content_type.startswith("application/json")
            ):
                try:
                    body = json.loads(response.content)
                    if isinstance(body, dict) and "detail" in body and isinstance(body["detail"], list):
                        # Log Django Ninja / Pydantic validation errors
                        self.client.validation_error_logger.log_validation_errors(
                            consumer=consumer,
                            method=request.method,
                            path=view.pattern,
                            detail=body["detail"],
                        )
                except json.JSONDecodeError:  # pragma: no cover
                    pass
        return response

    def get_view(self, request: HttpRequest) -> Optional[DjangoViewInfo]:
        resolver_match = resolve(request.path_info)
        return next((view for view in self.views if view.pattern == resolver_match.route), None)

    def get_consumer(self, request: HttpRequest) -> Optional[str]:
        if hasattr(request, "consumer_identifier"):
            return str(request.consumer_identifier)
        if self.config is not None and self.config.identify_consumer_callback is not None:
            consumer_identifier = self.config.identify_consumer_callback(request)
            if consumer_identifier is not None:
                return str(consumer_identifier)
        if hasattr(request, "auth") and isinstance(request.auth, KeyInfo):
            return f"key:{request.auth.key_id}"
        return None


@dataclass
class DjangoViewInfo:
    func: Callable
    pattern: str
    name: Optional[str] = None

    @property
    def is_api_view(self) -> bool:
        return self.is_rest_framework_api_view or self.is_ninja_path_view

    @property
    def is_rest_framework_api_view(self) -> bool:
        try:
            from rest_framework.views import APIView

            return hasattr(self.func, "view_class") and issubclass(self.func.view_class, APIView)
        except ImportError:  # pragma: no cover
            return False

    @property
    def is_ninja_path_view(self) -> bool:
        try:
            from ninja.operation import PathView

            return hasattr(self.func, "__self__") and isinstance(self.func.__self__, PathView)
        except ImportError:  # pragma: no cover
            return False

    @property
    def allowed_methods(self) -> List[str]:
        if hasattr(self.func, "view_class"):
            return [method.upper() for method in self.func.view_class().allowed_methods]
        if self.is_ninja_path_view:
            assert hasattr(self.func, "__self__")
            return [method.upper() for operation in self.func.__self__.operations for method in operation.methods]
        return []  # pragma: no cover


def _get_app_info(
    views: List[DjangoViewInfo], app_version: Optional[str] = None, openapi_url: Optional[str] = None
) -> Dict[str, Any]:
    app_info: Dict[str, Any] = {}
    if openapi := _get_openapi(views, openapi_url):
        app_info["openapi"] = openapi
    if paths := _get_paths(views):
        app_info["paths"] = paths
    app_info["versions"] = _get_versions(app_version)
    app_info["client"] = "python:django"
    return app_info


def _get_paths(views: List[DjangoViewInfo]) -> List[Dict[str, str]]:
    return [
        {"method": method, "path": view.pattern}
        for view in views
        if view.is_api_view
        for method in view.allowed_methods
        if method not in ["HEAD", "OPTIONS"]
    ]


def _get_openapi(views: List[DjangoViewInfo], openapi_url: Optional[str] = None) -> Optional[str]:
    openapi_views = [
        view
        for view in views
        if (openapi_url is not None and view.pattern == openapi_url.removeprefix("/"))
        or (openapi_url is None and view.pattern.endswith("openapi.json") and "<" not in view.pattern)
    ]
    if len(openapi_views) == 1:
        rf = RequestFactory()
        request = rf.get(openapi_views[0].pattern)
        response = openapi_views[0].func(request)
        if response.status_code == 200:
            return response.content.decode()
    return None


def _extract_views_from_url_patterns(
    url_patterns: List[Any], base: str = "", namespace: Optional[str] = None
) -> List[DjangoViewInfo]:
    # Copied and adapted from django-extensions.
    # See https://github.com/django-extensions/django-extensions/blob/dd794f1b239d657f62d40f2c3178200978328ed7/django_extensions/management/commands/show_urls.py#L190C34-L190C34
    views = []
    for p in url_patterns:
        if isinstance(p, URLPattern):
            try:
                if not p.name:
                    name = p.name
                elif namespace:
                    name = f"{namespace}:{p.name}"
                else:
                    name = p.name
                views.append(DjangoViewInfo(func=p.callback, pattern=base + str(p.pattern), name=name))
            except ViewDoesNotExist:
                continue
        elif isinstance(p, URLResolver):
            try:
                patterns = p.url_patterns
            except ImportError:
                continue
            views.extend(
                _extract_views_from_url_patterns(
                    patterns,
                    base + str(p.pattern),
                    namespace=f"{namespace}:{p.namespace}" if namespace and p.namespace else p.namespace or namespace,
                )
            )
        elif hasattr(p, "_get_callback"):
            try:
                views.append(DjangoViewInfo(func=p._get_callback(), pattern=base + str(p.pattern), name=p.name))
            except ViewDoesNotExist:
                continue
        elif hasattr(p, "url_patterns"):
            try:
                patterns = p.url_patterns
            except ImportError:
                continue
            views.extend(
                _extract_views_from_url_patterns(
                    patterns,
                    base + str(p.pattern),
                    namespace=namespace,
                )
            )
    return views


def _get_versions(app_version: Optional[str]) -> Dict[str, str]:
    versions = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "apitally": version("apitally"),
        "django": version("django"),
    }
    try:
        versions["django-rest-framework"] = version("django-rest-framework")
    except PackageNotFoundError:  # pragma: no cover
        pass
    try:
        versions["django-ninja"] = version("django-ninja")
    except PackageNotFoundError:  # pragma: no cover
        pass
    if app_version:
        versions["app"] = app_version
    return versions
