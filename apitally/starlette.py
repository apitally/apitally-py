from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.starlette import StarletteInstrumentor
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Match
from starlette.schemas import SchemaGenerator

from apitally.shared import activation, config, startup
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
    app_version: str | None = None,
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
    """Set up Apitally for a Starlette application. Errors never propagate."""
    try:
        activation.configure(**config.explicit_kwargs(locals()))
        _instrument_app(app)
        startup.set_app_info(
            framework="starlette",
            paths=lambda: _get_paths(app),
            versions=startup.resolve_versions(app_version, starlette="starlette"),
        )
    except Exception:
        logger.exception("Apitally setup for Starlette failed")


def _instrument_app(app: Starlette) -> None:
    if any(m.cls is ApitallyASGIMiddleware for m in app.user_middleware):
        return
    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        # Pre-instrumented app: insert the transport middleware just inside the existing
        # OpenTelemetryMiddleware so it runs inside the SERVER span
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
