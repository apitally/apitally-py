from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.starlette import StarletteInstrumentor
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Match
from starlette.schemas import SchemaGenerator

from apitally.shared import activation, startup
from apitally.shared.asgi import ApitallyASGIMiddleware


if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan


__all__ = ["init_apitally"]

logger = logging.getLogger(__name__)


def init_apitally(
    app: Starlette,
    *,
    write_token: str | None = None,
    env: str | None = None,
    disabled: bool | None = None,
    app_version: str | None = None,
    capture_logs: bool = True,
    exclude_on_request: Callable[[ReadableSpan], bool] | None = None,
    exclude_on_response: Callable[[ReadableSpan], bool] | None = None,
    mask_request_body: Callable[[ReadableSpan, bytes], bytes | None] | None = None,
    mask_response_body: Callable[[ReadableSpan, bytes], bytes | None] | None = None,
    log_request_headers: bool = False,
    log_request_body: bool = False,
    log_response_headers: bool = False,
    log_response_body: bool = False,
    mask_query_params: list[str] | None = None,
    mask_headers: list[str] | None = None,
    mask_body_fields: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> None:
    """Set up Apitally for a Starlette application. Errors never propagate (design.md section 9)."""
    try:
        config_kwargs: dict[str, Any] = {
            "capture_logs": capture_logs,
            "exclude_on_request": exclude_on_request,
            "exclude_on_response": exclude_on_response,
            "mask_request_body": mask_request_body,
            "mask_response_body": mask_response_body,
            "log_request_headers": log_request_headers,
            "log_request_body": log_request_body,
            "log_response_headers": log_response_headers,
            "log_response_body": log_response_body,
            "mask_query_params": mask_query_params or [],
            "mask_headers": mask_headers or [],
            "mask_body_fields": mask_body_fields or [],
            "exclude_paths": exclude_paths or [],
        }
        # Omitted so the APITALLY_* env var fallbacks in config.resolve_config stay in effect
        if write_token is not None:
            config_kwargs["write_token"] = write_token
        if env is not None:
            config_kwargs["env"] = env
        if disabled is not None:
            config_kwargs["disabled"] = disabled
        activation.configure(**config_kwargs)
        _instrument_app(app)
        startup.set_app_info(
            framework="starlette",
            paths=lambda: _get_paths(app),
            versions=_get_versions(app_version),
        )
    except Exception:
        logger.exception("Apitally setup for Starlette failed")


def _instrument_app(app: Starlette) -> None:
    if any(m.cls is ApitallyASGIMiddleware for m in app.user_middleware):
        return
    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        # Pre-instrumented app: insert the transport middleware just inside the existing
        # OpenTelemetryMiddleware so it runs inside the SERVER span (design.md section 4)
        index = next(i for i, m in enumerate(app.user_middleware) if m.cls is OpenTelemetryMiddleware)
        app.user_middleware.insert(index + 1, Middleware(ApitallyASGIMiddleware, resolve_route=_resolve_route))
        app.user_middleware.insert(0, Middleware(activation.ASGIActivationShim))
        if app.middleware_stack is not None:
            app.middleware_stack = app.build_middleware_stack()
        return
    # add_middleware prepends, so each layer wraps the previous: shim -> instrumentor -> transport
    app.add_middleware(ApitallyASGIMiddleware, resolve_route=_resolve_route)
    StarletteInstrumentor.instrument_app(app)
    app.add_middleware(activation.ASGIActivationShim)


def _resolve_route(scope: dict[str, Any], routes: list[Any] | None = None) -> str | None:
    # Ported from the 0.x route matcher; returns the route template without root_path,
    # matching the http.route the instrumentor sets on the SERVER span
    if routes is None:
        app = scope.get("app")
        routes = getattr(app, "routes", None) or []
    for route in routes:
        if hasattr(route, "routes"):
            path = _resolve_route(scope, routes=route.routes)
            if path is not None:
                return path
        elif hasattr(route, "path"):
            match, _ = route.matches(scope)
            if match == Match.FULL:
                return route.path
    return None


def _get_paths(app: Starlette) -> list[dict[str, str]]:
    endpoints = SchemaGenerator({}).get_endpoints(app.routes)
    return [{"method": endpoint.http_method, "path": endpoint.path} for endpoint in endpoints]


def _get_versions(app_version: str | None) -> dict[str, str]:
    versions = {}
    try:
        versions["starlette"] = version("starlette")
    except PackageNotFoundError:
        pass
    if app_version:
        versions["app"] = str(app_version)
    return versions
