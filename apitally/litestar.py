from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from litestar.exceptions import HTTPException
from litestar.middleware.base import DefineMiddleware
from litestar.plugins import InitPluginProtocol
from litestar.plugins.opentelemetry import (
    OpenTelemetryConfig,
    OpenTelemetryInstrumentationMiddleware,
    OpenTelemetryPlugin,
)
from litestar.routes import HTTPRoute

from apitally.shared import activation, config, startup
from apitally.shared.asgi import ApitallyASGIMiddleware
from apitally.shared.helpers import capture_exception
from apitally.shared.span_processor import get_server_span


if TYPE_CHECKING:
    from litestar.app import Litestar
    from litestar.config.app import AppConfig
    from litestar.types import Message, Scope
    from opentelemetry.sdk.trace import ReadableSpan


__all__ = ["ApitallyPlugin"]

logger = logging.getLogger(__name__)


class ApitallyPlugin(InitPluginProtocol):
    def __init__(
        self,
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
        self.configure_kwargs = config.explicit_kwargs(locals())
        self.app_version = app_version

    def on_app_init(self, app_config: AppConfig) -> AppConfig:
        try:
            cfg = activation.configure(**self.configure_kwargs)
            if cfg.disabled:
                return app_config
            if not _has_otel_instrumentation(app_config):
                # Install via the plugin registry, never the middleware list: with two
                # OpenTelemetry configs in the middleware list only the last one takes effect,
                # so a user's existing config would be silently discarded
                otel_plugin = OpenTelemetryPlugin(OpenTelemetryConfig(exclude_spans=["receive", "send"]))
                app_config.plugins = [*app_config.plugins, otel_plugin]
            app_config.middleware.append(
                DefineMiddleware(ApitallyASGIMiddleware, resolve_route=_resolve_route)  # ty: ignore[invalid-argument-type]
            )
            app_config.before_send.append(_before_send)
            app_config.after_exception.append(_after_exception)
            app_config.on_startup.append(self.on_startup)
        except Exception:  # pragma: no cover
            logger.exception("Error initializing Apitally for Litestar")
        return app_config

    def on_startup(self, app: Litestar) -> None:
        try:
            startup.set_app_info(
                framework="litestar",
                paths=lambda: _get_paths(app),
                versions=startup.resolve_versions(self.app_version, litestar="litestar"),
                openapi=lambda: _get_openapi(app),
            )
            activation.activate()
        except Exception:  # pragma: no cover
            logger.exception("Error initializing Apitally for Litestar")


def _has_otel_instrumentation(app_config: AppConfig) -> bool:
    # Covers both the plugin and the older pattern of adding the OTel middleware directly
    return any(isinstance(plugin, OpenTelemetryPlugin) for plugin in app_config.plugins) or any(
        isinstance(middleware, DefineMiddleware)
        and isinstance(middleware.middleware, type)
        and issubclass(middleware.middleware, OpenTelemetryInstrumentationMiddleware)
        for middleware in app_config.middleware
    )


async def _before_send(message: Message, scope: Scope) -> None:
    """Repair http.route and the span name from the routed template."""
    try:
        if message.get("type") != "http.response.start":
            return
        path_template = scope.get("path_template")
        span = get_server_span()
        if path_template and span is not None and span.is_recording():
            # The bare template per semconv; the method prefix belongs only in the span name
            span.set_attribute("http.route", str(path_template))
            span.update_name(f"{scope.get('method', '')} {path_template}".strip())
    except Exception:  # pragma: no cover
        logger.exception("Error in Apitally before_send hook")


def _after_exception(exception: Exception, scope: Scope) -> None:
    """Litestar turns handler exceptions into responses before the OTel middleware sees anything raised."""
    if isinstance(exception, HTTPException) and exception.status_code < 500:
        return
    capture_exception(exception)


def _resolve_route(scope: Scope) -> str | None:
    path_template = scope.get("path_template")
    return str(path_template) if path_template else None


def _get_paths(app: Litestar) -> list[dict[str, str]]:
    openapi_path = _get_openapi_path(app)
    return [
        {"method": method, "path": route.path_format}
        for route in app.routes
        if isinstance(route, HTTPRoute) and not _is_openapi_path(route.path_format, openapi_path)
        for method in sorted(route.methods)
        if method not in ("OPTIONS", "HEAD")
    ]


def _get_openapi_path(app: Litestar) -> str | None:
    config = app.openapi_config
    if config is None:
        return None
    if config.openapi_controller is not None:
        return config.openapi_controller.path
    router = getattr(config, "openapi_router", None)
    if router is not None:
        return router.path
    return config.path or "/schema"


def _is_openapi_path(path: str, openapi_path: str | None) -> bool:
    return openapi_path is not None and (path == openapi_path or path.startswith(openapi_path + "/"))


def _get_openapi(app: Litestar) -> str | None:
    if app.openapi_config is None:
        return None
    return json.dumps(app.openapi_schema.to_schema())
