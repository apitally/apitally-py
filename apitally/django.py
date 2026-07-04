from __future__ import annotations

import json
import logging
import re
import sys
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

import django
from django.conf import settings
from django.contrib.admindocs.views import extract_views_from_urlpatterns, simplify_regex
from django.core.signals import request_started
from django.urls import get_resolver
from django.utils.encoding import force_str
from django.utils.functional import Promise
from django.views.generic.base import View
from opentelemetry.instrumentation.django import DjangoInstrumentor

from apitally.shared import activation, metrics, startup
from apitally.shared.config import ApitallyConfig, get_config
from apitally.shared.consumer import get_consumer_identifier, reset_consumer_identifier
from apitally.shared.redaction import REDACTED, Redaction
from apitally.shared.span_processor import get_server_span
from apitally.shared.wsgi import (
    BODY_MASKED,
    BODY_TOO_LARGE,
    MAX_BODY_SIZE,
    is_allowed_content_type,
    parse_content_length,
)


if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse
    from opentelemetry.sdk.trace import ReadableSpan, Span


__all__ = ["init_apitally"]

logger = logging.getLogger(__name__)

APITALLY_MIDDLEWARE = "apitally.django.ApitallyDjangoMiddleware"
OTEL_MIDDLEWARE = DjangoInstrumentor._opentelemetry_middleware
PATH_PARAMETER_RE = re.compile(r"<(?:[^>:]+:)?(?P<parameter>\w+)>")

_urlconfs: list[str | None] = [None]
_include_django_views = False


def init_apitally(
    *,
    app_version: str | None = None,
    urlconf: str | list[str | None] | None = None,
    include_django_views: bool = False,
    **kwargs: Any,
) -> None:
    """Set up Apitally for Django; call at the end of settings.py, after MIDDLEWARE is defined."""
    global _urlconfs, _include_django_views
    caller_globals = sys._getframe(1).f_globals
    if isinstance(caller_globals.get("MIDDLEWARE"), tuple):
        # A list is required so the middleware insertions below mutate the settings module in place
        caller_globals["MIDDLEWARE"] = list(caller_globals["MIDDLEWARE"])
    activation.configure(**kwargs)
    _urlconfs = [urlconf] if urlconf is None or isinstance(urlconf, str) else urlconf
    _include_django_views = include_django_views
    instrumentor = DjangoInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument()
    _insert_middleware(caller_globals)
    request_started.connect(_handle_request_started, weak=False, dispatch_uid="apitally")
    startup.set_app_info(
        framework="django", paths=_get_paths, versions=_get_versions(app_version), openapi=_get_openapi
    )


def _insert_middleware(caller_globals: dict[str, Any]) -> None:
    middleware = caller_globals.get("MIDDLEWARE")
    if not isinstance(middleware, list) and settings.configured:
        middleware = settings.MIDDLEWARE
        if isinstance(middleware, tuple):
            middleware = list(middleware)
            settings.MIDDLEWARE = middleware
    if not isinstance(middleware, list):
        logger.warning("Apitally could not find the MIDDLEWARE setting, requests will not be tracked")
        return
    if APITALLY_MIDDLEWARE not in middleware:
        position = middleware.index(OTEL_MIDDLEWARE) + 1 if OTEL_MIDDLEWARE in middleware else 0
        middleware.insert(position, APITALLY_MIDDLEWARE)


def _handle_request_started(sender: Any, **kwargs: Any) -> None:
    if not activation.activation_attempted:
        activation.activate()


class ApitallyDjangoMiddleware:
    """Transport glue running inside the OTel Django middleware, while the SERVER span records."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.config: ApitallyConfig | None = None
        self.redaction = Redaction()

    def __call__(self, request: HttpRequest) -> HttpResponse:
        config = self.refresh_config()
        start_time = time.perf_counter()
        request_size: int | None = None
        request_body: bytes | str | None = None
        try:
            reset_consumer_identifier()
            request_size = parse_content_length(request.headers.get("Content-Length"))
            request_body = self.capture_request_body(request, config, request_size)
        except Exception:
            logger.exception("Error in Apitally Django middleware")
        response = self.get_response(request)
        try:
            self.finalize(request, response, config, start_time, request_size, request_body)
        except Exception:
            logger.exception("Error in Apitally Django middleware")
        return response

    def capture_request_body(
        self, request: HttpRequest, config: ApitallyConfig, request_size: int | None
    ) -> bytes | str | None:
        if not config.log_request_body or not is_allowed_content_type(request.headers.get("Content-Type")):
            return None
        if request_size is None:
            return None
        if request_size > MAX_BODY_SIZE:
            return BODY_TOO_LARGE
        body = request.body
        return body if len(body) <= MAX_BODY_SIZE else BODY_TOO_LARGE

    def capture_response_body(
        self, response: HttpResponse, config: ApitallyConfig, response_size: int | None, streaming: bool
    ) -> bytes | str | None:
        if not config.log_response_body or streaming or not is_allowed_content_type(response.get("Content-Type")):
            return None
        if response_size is not None and response_size > MAX_BODY_SIZE:
            return BODY_TOO_LARGE
        return response.content

    def finalize(
        self,
        request: HttpRequest,
        response: HttpResponse,
        config: ApitallyConfig,
        start_time: float,
        request_size: int | None,
        request_body: bytes | str | None,
    ) -> None:
        duration = time.perf_counter() - start_time
        streaming = getattr(response, "streaming", False)
        response_size = parse_content_length(response.get("Content-Length"))
        if response_size is None and not streaming:
            response_size = len(response.content)
        route = self.get_route(request)
        span = get_server_span()
        if span is not None and span.is_recording():
            if route is not None:
                # Overwrites the instrumentor's raw route so spans and metrics agree on the template
                span.set_attribute("http.route", route)
            if request_size is not None:
                span.set_attribute("http.request.body.size", request_size)
            if response_size is not None:
                span.set_attribute("http.response.body.size", response_size)
            if config.log_request_headers:
                self.set_header_attributes(span, "http.request.header.", request.headers.items())
            if config.log_response_headers:
                self.set_header_attributes(span, "http.response.header.", response.items())
            if request_body is not None:
                self.set_body_attribute(
                    span, "apitally.request.body", request_body, config.mask_request_body, "mask_request_body"
                )
            response_body = self.capture_response_body(response, config, response_size, streaming)
            if response_body is not None:
                self.set_body_attribute(
                    span, "apitally.response.body", response_body, config.mask_response_body, "mask_response_body"
                )
        metrics.record_request(
            method=request.method or "",
            route=route or "",
            status_code=response.status_code,
            consumer=get_consumer_identifier(),
            duration=duration,
            request_size=request_size,
            response_size=response_size,
            scheme=request.scheme,
        )

    def get_route(self, request: HttpRequest) -> str | None:
        match = request.resolver_match
        if match is not None and match.route:
            return _transform_path(match.route)
        return None

    def set_body_attribute(
        self,
        span: Span,
        key: str,
        body: bytes | str,
        mask_callback: Callable[[ReadableSpan, bytes], bytes | None] | None,
        callback_name: str,
    ) -> None:
        if isinstance(body, str):
            span.set_attribute(key, body)
            return
        if mask_callback is not None:
            try:
                masked = mask_callback(span, body)
            except Exception:
                logger.warning(
                    "Apitally %s callback raised an exception, body replaced with %s",
                    callback_name,
                    BODY_MASKED,
                    exc_info=True,
                )
                masked = None
            if masked is None:
                span.set_attribute(key, BODY_MASKED)
                return
            if len(masked) > MAX_BODY_SIZE:
                span.set_attribute(key, BODY_TOO_LARGE)
                return
            body = masked
        try:
            value = json.dumps(self.redaction.redact_body(json.loads(body)))
        except Exception:
            value = body.decode("utf-8", errors="replace")
        span.set_attribute(key, value)

    def set_header_attributes(self, span: Span, prefix: str, headers: Iterable[tuple[str, str]]) -> None:
        for name, value in headers:
            name = name.lower()
            values = [REDACTED] if self.redaction.should_redact_header(name) else [value]
            span.set_attribute(prefix + name, values)

    def refresh_config(self) -> ApitallyConfig:
        config = get_config() or ApitallyConfig()
        if config is not self.config:
            self.config = config
            self.redaction = Redaction(config.mask_query_params, config.mask_headers, config.mask_body_fields)
        return config


def _transform_path(path: str) -> str:
    return PATH_PARAMETER_RE.sub(r"{\g<parameter>}", simplify_regex(path))


def _get_versions(app_version: str | None) -> dict[str, str]:
    versions = {"django": django.get_version()}
    for package in ("djangorestframework", "django-ninja"):
        with suppress(PackageNotFoundError):
            versions[package] = version(package)
    if app_version:
        versions["app"] = app_version
    return versions


def _get_paths() -> list[dict[str, str]]:
    paths: list[dict[str, str]] = []
    with suppress(ImportError):
        from apitally.django_rest_framework import _get_drf_paths

        paths.extend(_get_drf_paths(_urlconfs))
    with suppress(ImportError):
        from apitally.django_ninja import _get_ninja_paths

        paths.extend(_get_ninja_paths(_urlconfs))
    if _include_django_views:
        paths.extend(_get_django_paths(_urlconfs))
    seen: set[tuple[str, str]] = set()
    deduplicated = []
    for path in paths:
        key = (path["method"], path["path"])
        if key not in seen:
            seen.add(key)
            deduplicated.append(path)
    return deduplicated


def _get_django_paths(urlconfs: list[str | None]) -> list[dict[str, str]]:
    return [
        {"method": method.upper(), "path": _transform_path(regex)}
        for urlconf in urlconfs
        for callback, regex, _, _ in extract_views_from_urlpatterns(get_resolver(urlconf).url_patterns)
        if hasattr(callback, "view_class") and issubclass(callback.view_class, View)
        for method in callback.view_class.http_method_names
        if method != "options" and hasattr(callback.view_class, method)
    ]


def _get_openapi() -> str | None:
    drf_schema = None
    ninja_schema = None
    with suppress(ImportError):
        from apitally.django_rest_framework import _get_drf_schema, _get_drf_spectacular_schema

        schema_class = getattr(settings, "REST_FRAMEWORK", {}).get("DEFAULT_SCHEMA_CLASS", "")
        drf_schema = (
            _get_drf_spectacular_schema(_urlconfs)
            if schema_class == "drf_spectacular.openapi.AutoSchema"
            else _get_drf_schema(_urlconfs)
        )
    with suppress(ImportError):
        from apitally.django_ninja import _get_ninja_schema

        ninja_schema = _get_ninja_schema(_urlconfs)
    if drf_schema is not None and ninja_schema is None:
        return json.dumps(_convert_proxy_objects(drf_schema))
    if ninja_schema is not None and drf_schema is None:
        return json.dumps(_convert_proxy_objects(ninja_schema))
    return None


def _convert_proxy_objects(data: Any) -> Any:
    """Recursively convert Django lazy proxy objects to strings to make them JSON serializable."""
    if isinstance(data, Promise):
        return force_str(data)
    if isinstance(data, dict):
        return {key: _convert_proxy_objects(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [_convert_proxy_objects(item) for item in data]
    return data
