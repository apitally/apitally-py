from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Union

from django.conf import settings
from django.urls import URLPattern, URLResolver, get_resolver
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
    urlconfs: List[Optional[str]]


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
        self.client.set_app_info(
            app_info=_get_app_info(
                app_version=self.config.app_version,
                urlconfs=self.config.urlconfs,
            )
        )

    @classmethod
    def configure(
        cls,
        client_id: str,
        env: str = "dev",
        app_version: Optional[str] = None,
        identify_consumer_callback: Optional[str] = None,
        urlconf: Optional[Union[List[Optional[str]], str]] = None,
    ) -> None:
        cls.config = ApitallyMiddlewareConfig(
            client_id=client_id,
            env=env,
            app_version=app_version,
            identify_consumer_callback=import_string(identify_consumer_callback)
            if identify_consumer_callback
            else None,
            urlconfs=[urlconf] if urlconf is None or isinstance(urlconf, str) else urlconf,
        )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        start_time = time.perf_counter()
        response = self.get_response(request)
        response_time = time.perf_counter() - start_time
        path = self.get_path(request)
        if request.method is not None and path is not None:
            consumer = self.get_consumer(request)
            try:
                self.client.request_counter.add_request(
                    consumer=consumer,
                    method=request.method,
                    path=path,
                    status_code=response.status_code,
                    response_time=response_time,
                    request_size=request.headers.get("Content-Length"),
                    response_size=response["Content-Length"]
                    if response.has_header("Content-Length")
                    else (len(response.content) if not response.streaming else None),
                )
            except Exception:  # pragma: no cover
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
                except Exception:  # pragma: no cover
                    logger.exception("Failed to log validation errors")
        return response

    def get_path(self, request: HttpRequest) -> Optional[str]:
        if (match := request.resolver_match) is not None:
            try:
                if self.drf_endpoint_enumerator is not None:
                    from rest_framework.schemas.generators import is_api_view

                    if is_api_view(match.func):
                        return self.drf_endpoint_enumerator.get_path_from_regex(match.route)
                if self.ninja_available:
                    from ninja.operation import PathView

                    if hasattr(match.func, "__self__") and isinstance(match.func.__self__, PathView):
                        path = "/" + match.route.lstrip("/")
                        return re.sub(r"<(?:[^:]+:)?([^>:]+)>", r"{\1}", path)
            except Exception:  # pragma: no cover
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
        except Exception:  # pragma: no cover
            logger.exception("Failed to get consumer identifier for request")
        return None


def _get_app_info(app_version: Optional[str], urlconfs: List[Optional[str]]) -> Dict[str, Any]:
    app_info: Dict[str, Any] = {}
    try:
        app_info["paths"] = _get_paths(urlconfs)
    except Exception:  # pragma: no cover
        app_info["paths"] = []
        logger.exception("Failed to get paths")
    try:
        app_info["openapi"] = _get_openapi(urlconfs)
    except Exception:  # pragma: no cover
        logger.exception("Failed to get OpenAPI schema")
    app_info["versions"] = get_versions("django", "djangorestframework", "django-ninja", app_version=app_version)
    app_info["client"] = "python:django"
    return app_info


def _get_openapi(urlconfs: List[Optional[str]]) -> Optional[str]:
    drf_schema = None
    ninja_schema = None
    with contextlib.suppress(ImportError):
        drf_schema = _get_drf_schema(urlconfs)
    with contextlib.suppress(ImportError):
        ninja_schema = _get_ninja_schema(urlconfs)
    if drf_schema is not None and ninja_schema is None:
        return json.dumps(drf_schema)
    elif ninja_schema is not None and drf_schema is None:
        return json.dumps(ninja_schema)
    return None  # pragma: no cover


def _get_paths(urlconfs: List[Optional[str]]) -> List[Dict[str, str]]:
    paths = []
    with contextlib.suppress(ImportError):
        paths.extend(_get_drf_paths(urlconfs))
    with contextlib.suppress(ImportError):
        paths.extend(_get_ninja_paths(urlconfs))
    return paths


def _get_drf_paths(urlconfs: List[Optional[str]]) -> List[Dict[str, str]]:
    from rest_framework.schemas.generators import EndpointEnumerator

    enumerators = [EndpointEnumerator(urlconf=urlconf) for urlconf in urlconfs]
    return [
        {
            "method": method.upper(),
            "path": path,
        }
        for enumerator in enumerators
        for path, method, _ in enumerator.get_api_endpoints()
        if method not in ["HEAD", "OPTIONS"]
    ]


def _get_drf_schema(urlconfs: List[Optional[str]]) -> Optional[Dict[str, Any]]:
    from rest_framework.schemas.openapi import SchemaGenerator

    schemas = []
    with contextlib.suppress(AssertionError):  # uritemplate and inflection must be installed for OpenAPI schema support
        for urlconf in urlconfs:
            generator = SchemaGenerator(urlconf=urlconf)
            schema = generator.get_schema()
            if schema is not None and len(schema["paths"]) > 0:
                schemas.append(schema)
    return None if len(schemas) != 1 else schemas[0]  # type: ignore[return-value]


def _get_ninja_paths(urlconfs: List[Optional[str]]) -> List[Dict[str, str]]:
    endpoints = []
    for api in _get_ninja_api_instances(urlconfs=urlconfs):
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


def _get_ninja_schema(urlconfs: List[Optional[str]]) -> Optional[Dict[str, Any]]:
    schemas = []
    for api in _get_ninja_api_instances(urlconfs=urlconfs):
        schema = api.get_openapi_schema()
        if len(schema["paths"]) > 0:
            schemas.append(schema)
    return None if len(schemas) != 1 else schemas[0]


def _get_ninja_api_instances(
    urlconfs: Optional[List[Optional[str]]] = None,
    patterns: Optional[List[Any]] = None,
) -> Set[NinjaAPI]:
    from ninja import NinjaAPI

    if urlconfs is None:
        urlconfs = [None]
    if patterns is None:
        patterns = []
        for urlconf in urlconfs:
            patterns.extend(get_resolver(urlconf).url_patterns)

    apis: Set[NinjaAPI] = set()
    for p in patterns:
        if isinstance(p, URLResolver):
            if p.app_name != "ninja":
                apis.update(_get_ninja_api_instances(patterns=p.url_patterns))
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
