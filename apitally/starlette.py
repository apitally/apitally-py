from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.trace import Status, StatusCode
from opentelemetry.util.http import get_excluded_urls
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.errors import ServerErrorMiddleware
from starlette.routing import Match
from starlette.schemas import SchemaGenerator

from apitally.shared import activation, config, startup
from apitally.shared.asgi import ApitallyASGIMiddleware


if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan
    from starlette.routing import BaseRoute
    from starlette.types import ASGIApp, Receive, Scope, Send


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
    """Set up Apitally for a Starlette application. Activation happens on lifespan startup or the first request."""
    try:
        cfg = activation.configure(**config.explicit_kwargs(locals()))
        if cfg.disabled:
            return
        _instrument_app(app)
        startup.set_app_info(
            framework="starlette",
            paths=lambda: _get_paths(app),
            versions=startup.resolve_versions(app_version, starlette="starlette"),
        )
    except Exception:  # pragma: no cover
        logger.exception("Apitally setup for Starlette failed")


def _instrument_app(app: Starlette) -> None:
    if getattr(app, "_is_instrumented_by_apitally", False):
        return
    setattr(app, "_is_instrumented_by_apitally", True)
    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        # Pre-instrumented app: insert the transport middleware just inside the existing
        # OpenTelemetryMiddleware so it runs inside the SERVER span
        index = next(i for i, m in enumerate(app.user_middleware) if m.cls is OpenTelemetryMiddleware)
        app.user_middleware.insert(index + 1, Middleware(ApitallyASGIMiddleware, resolve_route=_resolve_route))
        app.user_middleware.insert(0, Middleware(activation.ASGIActivationShim))
        if app.middleware_stack is not None:
            app.middleware_stack = app.build_middleware_stack()
        return
    setattr(app, "_is_instrumented_by_opentelemetry", True)

    # Replacing build_middleware_stack puts the transport middleware outside
    # ServerErrorMiddleware, so 500 responses to unhandled exceptions pass through it
    build_inner = app.build_middleware_stack

    def build_with_shim() -> activation.ASGIActivationShim:
        inner = build_inner()
        if isinstance(inner, ServerErrorMiddleware):
            inner.app = _ExceptionRecordingMiddleware(inner.app)
        return activation.ASGIActivationShim(
            ApitallyASGIMiddleware(
                # Composed directly instead of via StarletteInstrumentor.instrument_app, which
                # does not accept exclude_spans and would create two receive/send spans per request
                OpenTelemetryMiddleware(  # ty: ignore[invalid-argument-type]
                    inner,
                    excluded_urls=get_excluded_urls("STARLETTE"),
                    default_span_details=_get_default_span_details,
                    exclude_spans=["receive", "send"],
                ),
                resolve_route=_resolve_route,
            )
        )

    app.build_middleware_stack = build_with_shim  # ty: ignore[invalid-assignment]


class _ExceptionRecordingMiddleware:
    """Records unhandled exceptions before ServerErrorMiddleware sends the 500 response,
    which ends the SERVER span."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            span = trace.get_current_span()
            if span.is_recording():
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            raise


def _get_default_span_details(scope: Scope) -> tuple[str, dict[str, Any]]:
    route = _resolve_route(scope)
    method = str(scope.get("method", ""))
    if route is None:
        return method, {}
    return f"{method} {route}".strip(), {"http.route": route}


def _resolve_route(scope: Scope, routes: list[BaseRoute] | None = None) -> str | None:
    # Returns the route template without the mount prefix; the transport middleware
    # restores the prefix from the root_path delta
    if routes is None:
        app = scope.get("app")
        routes = getattr(app, "routes", None) or []
    for route in routes:
        sub_routes = getattr(route, "routes", None)
        if sub_routes is not None:
            path = _resolve_route(scope, routes=sub_routes)
            if path is not None:
                return path
        elif (path := getattr(route, "path", None)) is not None:
            match, _ = route.matches(scope)
            if match == Match.FULL:
                return path
    return None  # pragma: no cover


def _get_paths(app: Starlette) -> list[dict[str, str]]:
    endpoints = SchemaGenerator({}).get_endpoints(app.routes)
    return [{"method": endpoint.http_method, "path": endpoint.path} for endpoint in endpoints]
