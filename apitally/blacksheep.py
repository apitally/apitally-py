from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from blacksheep import Application
from blacksheep.messages import Request, Response
from blacksheep.server.openapi.v3 import Info, OpenAPIHandler, Operation
from blacksheep.server.routing import RouteMatch
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

from apitally.shared import activation, config, startup
from apitally.shared.asgi import ApitallyASGIMiddleware
from apitally.shared.span_processor import get_server_span


if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan


__all__ = ["init_apitally"]

logger = logging.getLogger(__name__)


def init_apitally(
    app: Application | OpenTelemetryMiddleware,
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
    """
    Set up Apitally for a BlackSheep application.

    For more information, see:
    - Setup guide: https://docs.apitally.io/frameworks/blacksheep
    - Reference: https://docs.apitally.io/reference/python
    """
    try:
        cfg = activation.configure(**config.explicit_kwargs(locals()))
        if cfg.disabled:
            return
        instrumented_by_user = isinstance(app, OpenTelemetryMiddleware)
        if instrumented_by_user:
            # The user wrapped the app in their own generic ASGI instrumentor; reuse their
            # SERVER spans instead of nesting a second one
            app = app.app

        if getattr(app, "_apitally_initialized", False):
            return

        _wrap_router(app)
        _wrap_error_handler(app)

        # Python looks up __call__ on the class, not the instance, so a wrapper assigned to
        # app.__call__ would never be called; wrapping app._handle_http works
        chain: ApitallyASGIMiddleware | OpenTelemetryMiddleware = ApitallyASGIMiddleware(app._handle_http)
        if not instrumented_by_user:
            chain = OpenTelemetryMiddleware(chain, exclude_spans=["receive", "send"])
        app._handle_http = activation.ASGIActivationShim(chain)  # ty: ignore[invalid-assignment]
        app._apitally_initialized = True  # ty: ignore[invalid-assignment]

        async def activate_on_start(application: Application) -> None:
            activation.activate()

        app.on_start += activate_on_start

        startup.set_app_info(
            framework="blacksheep",
            paths=lambda: _get_paths(app),
            versions=startup.resolve_versions(app_version, blacksheep="blacksheep"),
        )
    except Exception:  # pragma: no cover
        logger.exception("Error setting up Apitally for BlackSheep")


def _wrap_router(app: Application) -> None:
    original_get_match = app.router.get_match

    def get_match(request: Request) -> RouteMatch | None:
        match = original_get_match(request)
        try:
            # The "*" fallback pattern matches unmatched requests (BlackSheep >= 2.4.4)
            if match is not None and (route := match.pattern.decode()) != "*":
                if isinstance(scope := request.scope, dict):
                    scope["route"] = route  # ty: ignore[invalid-key]
                span = get_server_span()
                if span is not None and span.is_recording():
                    span.set_attribute("http.route", route)
                    span.update_name(f"{request.method} {route}")
        except Exception:  # pragma: no cover
            logger.exception("Error resolving route in Apitally BlackSheep integration")
        return match

    app.router.get_match = get_match  # ty: ignore[invalid-assignment]


def _wrap_error_handler(app: Application) -> None:
    # BlackSheep converts unhandled handler exceptions into 500 responses before the
    # instrumentor sees anything raised; handle_internal_server_error is reached exactly
    # for those (HTTPExceptions and user-handled exceptions route elsewhere), so record
    # the exception on the SERVER span there
    original = app.handle_internal_server_error

    async def handle_internal_server_error(request: Request, exc: Exception) -> Response:
        try:
            span = get_server_span()
            if span is not None and span.is_recording():
                span.record_exception(exc)
        except Exception:  # pragma: no cover
            logger.exception("Error recording exception in Apitally BlackSheep integration")
        return await original(request, exc)

    app.handle_internal_server_error = handle_internal_server_error  # ty: ignore[invalid-assignment]


def _get_paths(app: Application) -> list[dict[str, str]]:
    openapi = OpenAPIHandler(info=Info(title="", version=""))
    path_items = openapi.get_paths(app)
    routers = list(app.router._sub_routers or [])
    while routers:
        router = routers.pop()
        path_items.update(openapi.get_routes_docs(router))
        routers.extend(router._sub_routers or [])
    methods = ("get", "put", "post", "delete", "patch")
    paths = []
    for path, path_item in path_items.items():
        for method in methods:
            operation: Operation | None = getattr(path_item, method, None)
            if operation is not None:
                item = {"method": method.upper(), "path": path}
                if operation.summary:
                    item["summary"] = operation.summary
                if operation.description:
                    item["description"] = operation.description
                paths.append(item)
    return paths
