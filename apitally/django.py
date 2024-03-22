from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set

from django.conf import settings
from django.urls import Resolver404, URLPattern, URLResolver, get_resolver, resolve
from django.utils.module_loading import import_string

from apitally.client.logging import get_logger
from apitally.client.threading import ApitallyClient
from apitally.common import get_versions


if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse
    from ninja import NinjaAPI


__all__ = ["ApitallyMiddleware"]
logger = get_logger(__name__)


@dataclass
class ApitallyMiddlewareConfig:
    client_id: str
    env: str
    app_version: Optional[str]
    identify_consumer_callback: Optional[Callable[[HttpRequest], Optional[str]]]


class ApitallyMiddleware:
    config: Optional[ApitallyMiddlewareConfig] = None

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.ninja_available = _check_import("ninja")
        self.drf_endpoint_enumerator = None
        if _check_import("rest_framework"):
            from rest_framework.schemas.generators import EndpointEnumerator

            self.drf_endpoint_enumerator = EndpointEnumerator()

        if self.config is None:
            config = getattr(settings, "APITALLY_MIDDLEWARE", {})
            self.configure(**config)
            assert self.config is not None

        self.client = ApitallyClient(client_id=self.config.client_id, env=self.config.env)
        self.client.start_sync_loop()
        self.client.set_app_info(app_info=_get_app_info(app_version=self.config.app_version))

    @classmethod
    def configure(
        cls,
        client_id: str,
        env: str = "dev",
        app_version: Optional[str] = None,
        identify_consumer_callback: Optional[str] = None,
    ) -> None:
        cls.config = ApitallyMiddlewareConfig(
            client_id=client_id,
            env=env,
            app_version=app_version,
            identify_consumer_callback=import_string(identify_consumer_callback)
            if identify_consumer_callback
            else None,
        )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        path = self.get_path(request)
        start_time = time.perf_counter()
        response = self.get_response(request)
        if request.method is not None and path is not None:
            try:
                consumer = self.get_consumer(request)
                self.client.request_counter.add_request(
                    consumer=consumer,
                    method=request.method,
                    path=path,
                    status_code=response.status_code,
                    response_time=time.perf_counter() - start_time,
                    request_size=request.headers.get("Content-Length"),
                    response_size=response["Content-Length"]
                    if response.has_header("Content-Length")
                    else (len(response.content) if not response.streaming else None),
                )
            except Exception:
                logger.exception("Failed to log request metadata")
            if (
                response.status_code == 422
                and (content_type := response.get("Content-Type")) is not None
                and content_type.startswith("application/json")
            ):
                try:
                    with contextlib.suppress(json.JSONDecodeError):
                        body = json.loads(response.content)
                        if isinstance(body, dict) and "detail" in body and isinstance(body["detail"], list):
                            # Log Django Ninja / Pydantic validation errors
                            self.client.validation_error_counter.add_validation_errors(
                                consumer=consumer,
                                method=request.method,
                                path=path,
                                detail=body["detail"],
                            )
                except Exception:
                    logger.exception("Failed to log validation errors")
        return response

    def get_path(self, request: HttpRequest) -> Optional[str]:
        try:
            resolver_match = resolve(request.path_info)
        except Resolver404:
            return None
        try:
            if self.drf_endpoint_enumerator is not None:
                from rest_framework.schemas.generators import is_api_view

                if is_api_view(resolver_match.func):
                    return self.drf_endpoint_enumerator.get_path_from_regex(resolver_match.route)
            if self.ninja_available:
                from ninja.operation import PathView

                if hasattr(resolver_match.func, "__self__") and isinstance(resolver_match.func.__self__, PathView):
                    path = "/" + resolver_match.route.lstrip("/")
                    return re.sub(r"<(?:[^:]+:)?([^>:]+)>", r"{\1}", path)
        except Exception:
            logger.exception("Failed to get path for request")
        return None

    def get_consumer(self, request: HttpRequest) -> Optional[str]:
        try:
            if hasattr(request, "consumer_identifier"):
                return str(request.consumer_identifier)
            if self.config is not None and self.config.identify_consumer_callback is not None:
                consumer_identifier = self.config.identify_consumer_callback(request)
                if consumer_identifier is not None:
                    return str(consumer_identifier)
        except Exception:
            logger.exception("Failed to get consumer identifier for request")
        return None


def _get_app_info(app_version: Optional[str] = None) -> Dict[str, Any]:
    app_info: Dict[str, Any] = {}
    try:
        app_info["paths"] = _get_paths()
    except Exception:
        app_info["paths"] = []
        logger.exception("Failed to get paths")
    try:
        app_info["openapi"] = _get_openapi()
    except Exception:
        logger.exception("Failed to get OpenAPI schema")
    app_info["versions"] = get_versions("django", "djangorestframework", "django-ninja", app_version=app_version)
    app_info["client"] = "python:django"
    return app_info


def _get_openapi() -> Optional[str]:
    drf_schema = None
    ninja_schema = None
    with contextlib.suppress(ImportError):
        drf_schema = _get_drf_schema()
    with contextlib.suppress(ImportError):
        ninja_schema = _get_ninja_schema()
    if drf_schema is not None and ninja_schema is None:
        return json.dumps(drf_schema)
    elif ninja_schema is not None and drf_schema is None:
        return json.dumps(ninja_schema)
    return None


def _get_paths() -> List[Dict[str, str]]:
    paths = []
    with contextlib.suppress(ImportError):
        paths.extend(_get_drf_paths())
    with contextlib.suppress(ImportError):
        paths.extend(_get_ninja_paths())
    return paths


def _get_drf_paths() -> List[Dict[str, str]]:
    from rest_framework.schemas.generators import EndpointEnumerator

    enumerator = EndpointEnumerator()
    return [
        {
            "method": method.upper(),
            "path": path,
        }
        for path, method, _ in enumerator.get_api_endpoints()
        if method not in ["HEAD", "OPTIONS"]
    ]


def _get_drf_schema() -> Optional[Dict[str, Any]]:
    from rest_framework.schemas.openapi import SchemaGenerator

    with contextlib.suppress(AssertionError):  # uritemplate must be installed for OpenAPI schema support
        generator = SchemaGenerator()
        schema = generator.get_schema()
        if schema is not None and len(schema["paths"]) > 0:
            return schema  # type: ignore[return-value]
    return None


def _get_ninja_paths() -> List[Dict[str, str]]:
    endpoints = []
    for api in _get_ninja_api_instances():
        schema = api.get_openapi_schema()
        for path, operations in schema["paths"].items():
            for method, operation in operations.items():
                if method not in ["HEAD", "OPTIONS"]:
                    endpoints.append(
                        {
                            "method": method,
                            "path": path,
                            "summary": operation.get("summary"),
                            "description": operation.get("description"),
                        }
                    )
    return endpoints


def _get_ninja_schema() -> Optional[Dict[str, Any]]:
    if len(apis := _get_ninja_api_instances()) == 1:
        api = list(apis)[0]
        schema = api.get_openapi_schema()
        if len(schema["paths"]) > 0:
            return schema
    return None


def _get_ninja_api_instances(url_patterns: Optional[List[Any]] = None) -> Set[NinjaAPI]:
    from ninja import NinjaAPI

    if url_patterns is None:
        url_patterns = get_resolver().url_patterns
    apis: Set[NinjaAPI] = set()
    for p in url_patterns:
        if isinstance(p, URLResolver):
            if p.app_name != "ninja":
                apis.update(_get_ninja_api_instances(p.url_patterns))
            else:
                for pattern in p.url_patterns:
                    if isinstance(pattern, URLPattern) and pattern.lookup_str.startswith("ninja."):
                        callback_keywords = getattr(pattern.callback, "keywords", {})
                        if isinstance(callback_keywords, dict):
                            api = callback_keywords.get("api")
                            if isinstance(api, NinjaAPI):
                                apis.add(api)
                                break
    return apis


def _check_import(name: str) -> bool:
    try:
        import_module(name)
        return True
    except ImportError:
        return False
