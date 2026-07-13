from __future__ import annotations

import json
import logging
import re
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import suppress
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

import django
from django.conf import settings
from django.contrib.admindocs.views import extract_views_from_urlpatterns, simplify_regex
from django.core.signals import request_started
from django.urls import get_resolver
from django.utils.encoding import force_str
from django.utils.functional import Promise
from django.views.generic.base import View
from opentelemetry.instrumentation.django import DjangoInstrumentor

from apitally.shared import activation, config, metrics, startup
from apitally.shared.config import (
    BODY_TOO_LARGE,
    MAX_BODY_SIZE,
    ApitallyConfig,
    get_config,
    is_allowed_content_type,
)
from apitally.shared.consumer import get_consumer_identifier, init_consumer, reset_consumer
from apitally.shared.context import get_server_span, get_server_span_processor, is_server_span_kept
from apitally.shared.wsgi import group_headers, parse_content_length


if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
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
    write_token: str | None = None,
    env: str | None = None,
    app_version: str | None = None,
    urlconf: str | list[str | None] | None = None,
    include_django_views: bool = False,
    disabled: bool | None = None,
    capture_logs: bool | None = None,
    log_request_headers: bool | None = None,
    log_request_body: bool | None = None,
    log_response_headers: bool | None = None,
    log_response_body: bool | None = None,
    mask_query_params: list[str] | None = None,
    mask_headers: list[str] | None = None,
    mask_body_fields: list[str] | None = None,
    mask_request_body: Callable[[ReadableSpan, bytes], bytes | None] | None = None,
    mask_response_body: Callable[[ReadableSpan, bytes], bytes | None] | None = None,
    exclude_paths: list[str] | None = None,
    sample_rate: float | None = None,
    sample_on_request: Callable[[ReadableSpan], float | bool | None] | None = None,
    sample_on_response: Callable[[ReadableSpan], float | bool | None] | None = None,
) -> None:
    """Set up Apitally for Django. Call this at the end of settings.py, after MIDDLEWARE is defined."""
    global _urlconfs, _include_django_views
    try:
        cfg = activation.configure(**config.explicit_kwargs(locals()))
        if cfg.disabled:
            return
        caller_globals = sys._getframe(1).f_globals
        if isinstance(caller_globals.get("MIDDLEWARE"), tuple):
            # A list is required so the middleware insertions below mutate the settings module in place
            caller_globals["MIDDLEWARE"] = list(caller_globals["MIDDLEWARE"])
        _urlconfs = [urlconf] if urlconf is None or isinstance(urlconf, str) else urlconf
        _include_django_views = include_django_views
        instrumentor = DjangoInstrumentor()
        if not instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.instrument()
        _insert_middleware(caller_globals)
        request_started.connect(_handle_request_started, weak=False, dispatch_uid="apitally")
        versions = {
            "django": django.get_version(),
            **startup.resolve_versions(
                app_version, djangorestframework="djangorestframework", **{"django-ninja": "django-ninja"}
            ),
        }
        startup.set_app_info(framework="django", paths=_get_paths, versions=versions, openapi=_get_openapi)
    except Exception:  # pragma: no cover
        logger.exception("Apitally setup for Django failed")


def _insert_middleware(caller_globals: dict[str, Any]) -> None:
    middleware = caller_globals.get("MIDDLEWARE")
    if not isinstance(middleware, list) and settings.configured:
        middleware = settings.MIDDLEWARE
        if isinstance(middleware, tuple):
            middleware = list(middleware)
            settings.MIDDLEWARE = middleware
    if not isinstance(middleware, list):  # pragma: no cover
        logger.warning("Apitally could not find the MIDDLEWARE setting, requests will not be tracked")
        return
    if APITALLY_MIDDLEWARE not in middleware:
        position = middleware.index(OTEL_MIDDLEWARE) + 1 if OTEL_MIDDLEWARE in middleware else 0
        middleware.insert(position, APITALLY_MIDDLEWARE)


def _handle_request_started(sender: object, **kwargs: Any) -> None:
    if not activation.activation_attempted:
        activation.activate()


class ApitallyDjangoMiddleware:
    """Sets Apitally span attributes and records metrics inside the OTel Django middleware, while the SERVER span is recording."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.config = get_config()

    def __call__(self, request: HttpRequest) -> HttpResponse:
        config = self.config
        start_time = time.perf_counter()
        request_size: int | None = None
        request_body: bytes | str | None = None
        try:
            init_consumer()
            request_size = parse_content_length(request.headers.get("Content-Length"))
            request_body = self.capture_request_body(request, config, request_size)
        except Exception:  # pragma: no cover
            logger.exception("Error in Apitally Django middleware")
        response = self.get_response(request)
        try:
            self.finalize(request, response, config, start_time, request_size, request_body)
        except Exception:  # pragma: no cover
            logger.exception("Error in Apitally Django middleware")
        finally:
            # finalize_streaming has already snapshotted the consumer for streaming responses
            reset_consumer()
        return response

    def capture_request_body(
        self, request: HttpRequest, config: ApitallyConfig, request_size: int | None
    ) -> bytes | str | None:
        # Excluded and sampled-out requests skip all capture work; metrics are still recorded
        if (
            not is_server_span_kept()
            or not config.log_request_body
            or not is_allowed_content_type(request.headers.get("Content-Type"))
        ):
            return None
        if request_size is None:  # pragma: no cover
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
        streaming = getattr(response, "streaming", False)
        response_size = parse_content_length(response.get("Content-Length"))
        if response_size is None and not streaming:
            response_size = len(response.content)
        route = self.get_route(request)
        span = get_server_span()
        if is_server_span_kept() and span is not None and span.is_recording():
            if route is not None:
                # Overwrites the instrumentor's raw route so spans and metrics agree on the template
                span.set_attribute("http.route", route)
            if request_size is not None:
                span.set_attribute("http.request.body.size", request_size)
            if response_size is not None:
                span.set_attribute("http.response.body.size", response_size)
            response_body = self.capture_response_body(response, config, response_size, streaming)
            if request_body is BODY_TOO_LARGE:
                span.set_attribute("apitally.request.body", BODY_TOO_LARGE)
            if response_body is BODY_TOO_LARGE:
                span.set_attribute("apitally.response.body", BODY_TOO_LARGE)
            stash_request_headers = group_headers(request.headers.items()) if config.log_request_headers else None
            stash_response_headers = group_headers(response.items()) if config.log_response_headers else None
            stash_request_body = request_body if isinstance(request_body, bytes) else None
            stash_response_body = response_body if isinstance(response_body, bytes) else None
            if (
                stash_request_headers or stash_request_body or stash_response_headers or stash_response_body
            ) and span.context is not None:
                processor = get_server_span_processor()
                if processor is not None:
                    processor.update_stash(
                        span.context.span_id,
                        request_headers=stash_request_headers,
                        request_body=stash_request_body,
                        response_headers=stash_response_headers,
                        response_body=stash_response_body,
                    )
        if streaming and response_size is None and not getattr(response, "is_async", False):
            self.finalize_streaming(
                request, cast("StreamingHttpResponse", response), config, start_time, request_size, route, span
            )
            return
        metrics.record_request(
            method=request.method or "",
            route=route or "",
            status_code=response.status_code,
            consumer=get_consumer_identifier(),
            duration=time.perf_counter() - start_time,
            request_size=request_size,
            response_size=response_size,
            scheme=request.scheme,
        )

    def finalize_streaming(
        self,
        request: HttpRequest,
        response: StreamingHttpResponse,
        config: ApitallyConfig,
        start_time: float,
        request_size: int | None,
        route: str | None,
        span: Span | None,
    ) -> None:
        """Defer the span export and record metrics once the streamed content completes,
        after the OTel middleware has ended the SERVER span."""
        processor = get_server_span_processor()
        span_id: int | None = None
        if processor is not None and is_server_span_kept() and span is not None and span.is_recording():
            context = span.context
            if context is not None:
                span_id = context.span_id
                processor.defer_export(span_id)
        capture_body = (
            span_id is not None and config.log_response_body and is_allowed_content_type(response.get("Content-Type"))
        )
        method = request.method or ""
        status_code = response.status_code
        consumer = get_consumer_identifier()
        scheme = request.scheme
        content = cast(Iterable[bytes], response.streaming_content)

        def stream_wrapper() -> Iterator[bytes]:
            bytes_sent = 0
            body: bytearray | str | None = bytearray() if capture_body else None
            completed = False
            try:
                for chunk in content:
                    bytes_sent += len(chunk)
                    if isinstance(body, bytearray):
                        body += chunk
                        if len(body) > MAX_BODY_SIZE:
                            body = BODY_TOO_LARGE
                    yield chunk
                completed = True
            finally:
                try:
                    response_size = bytes_sent if completed else None
                    extra: dict[str, str | int] = {}
                    if response_size is not None:
                        extra["http.response.body.size"] = response_size
                    # An abandoned iterator leaves a partial buffer; never export a truncated body
                    if completed and body is not None:
                        if body is BODY_TOO_LARGE:
                            extra["apitally.response.body"] = BODY_TOO_LARGE
                        elif isinstance(body, bytearray) and processor is not None and span_id is not None:
                            # The deferred export guarantees process_ended_span still runs and attaches this body
                            processor.update_stash(span_id, response_body=bytes(body))
                    if processor is not None and span_id is not None:
                        processor.finish_export(span_id, extra or None)
                    metrics.record_request(
                        method=method,
                        route=route or "",
                        status_code=status_code,
                        consumer=consumer,
                        duration=time.perf_counter() - start_time,
                        request_size=request_size,
                        response_size=response_size,
                        scheme=scheme,
                    )
                except Exception:  # pragma: no cover
                    logger.exception("Error in Apitally Django middleware")

        response.streaming_content = stream_wrapper()

    def get_route(self, request: HttpRequest) -> str | None:
        match = request.resolver_match
        if match is not None and match.route:
            return _regex_to_route_template(match.route)
        return None  # pragma: no cover


@lru_cache(1024)
def _regex_to_route_template(path: str) -> str:
    return PATH_PARAMETER_RE.sub(r"{\g<parameter>}", simplify_regex(path))


def _get_paths() -> list[dict[str, str]]:
    paths: list[dict[str, str]] = []
    with suppress(ImportError):
        from apitally.django_rest_framework import _get_drf_paths

        paths.extend(_get_drf_paths(_urlconfs))
    with suppress(ImportError):
        from apitally.django_ninja import _get_ninja_paths

        paths.extend(_get_ninja_paths(_urlconfs))
    if _include_django_views:
        paths.extend(_get_django_view_paths(_urlconfs))
    seen: set[tuple[str, str]] = set()
    deduplicated = []
    for path in paths:
        key = (path["method"], path["path"])
        if key not in seen:
            seen.add(key)
            deduplicated.append(path)
    return deduplicated


def _get_django_view_paths(urlconfs: list[str | None]) -> list[dict[str, str]]:
    return [
        {"method": method.upper(), "path": _regex_to_route_template(regex)}
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


ProxyValue = (
    str
    | int
    | float
    | bool
    | None
    | Promise
    | Mapping[str, "ProxyValue"]
    | list["ProxyValue"]
    | tuple["ProxyValue", ...]
)


def _convert_proxy_objects(data: ProxyValue) -> ProxyValue:
    """Recursively convert Django lazy proxy objects to strings to make them JSON serializable."""
    if isinstance(data, Promise):
        return force_str(data)
    if isinstance(data, dict):
        return {key: _convert_proxy_objects(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [_convert_proxy_objects(item) for item in data]
    return data
