from __future__ import annotations

import logging
from importlib.metadata import version
from typing import Any

from blacksheep import Application
from blacksheep.messages import Request
from blacksheep.server.openapi.v3 import Info, OpenAPIHandler, Operation
from blacksheep.server.routing import RouteMatch
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

from apitally.shared import activation, startup
from apitally.shared.asgi import ApitallyASGIMiddleware
from apitally.shared.span_processor import get_server_span


__all__ = ["init_apitally"]

logger = logging.getLogger(__name__)


def init_apitally(app: Application | OpenTelemetryMiddleware, app_version: str | None = None, **kwargs: Any) -> None:
    """
    Set up Apitally for a BlackSheep application.

    For more information, see:
    - Setup guide: https://docs.apitally.io/frameworks/blacksheep
    - Reference: https://docs.apitally.io/reference/python
    """
    try:
        activation.configure(**kwargs)
        instrumented_by_user = isinstance(app, OpenTelemetryMiddleware)
        if instrumented_by_user:
            # The user wrapped the app in their own generic ASGI instrumentor; reuse their
            # SERVER spans instead of nesting a second one (design.md section 4)
            logger.debug("Existing OpenTelemetryMiddleware detected, skipping Apitally instrumentor layer")
            app = app.app

        if getattr(app, "_apitally_initialized", False):
            return

        _wrap_router(app)

        # Per-instance __call__ assignment is ignored by Python's type-based dunder lookup,
        # so _handle_http is the interposition point (design.md section 4)
        chain: Any = ApitallyASGIMiddleware(app._handle_http)
        if not instrumented_by_user:
            chain = OpenTelemetryMiddleware(chain, exclude_spans=["receive", "send"])
        app._handle_http = activation.ASGIActivationShim(chain)  # ty: ignore[invalid-assignment]
        app._apitally_initialized = True  # ty: ignore[invalid-assignment]

        async def activate_on_start(application: Application) -> None:
            activation.activate()

        app.on_start += activate_on_start

        versions = {"blacksheep": version("blacksheep")}
        if app_version:
            versions["app"] = app_version
        startup.set_app_info(framework="blacksheep", paths=lambda: _get_paths(app), versions=versions)
    except Exception:
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
        except Exception:
            logger.exception("Error resolving route in Apitally BlackSheep integration")
        return match

    app.router.get_match = get_match  # ty: ignore[invalid-assignment]


def _get_paths(app: Application) -> list[dict[str, str]]:
    openapi = OpenAPIHandler(info=Info(title="", version=""))
    methods = ("get", "put", "post", "delete", "patch")
    paths = []
    for path, path_item in openapi.get_paths(app).items():
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
