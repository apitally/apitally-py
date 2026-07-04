from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from flask import Flask, Response
from opentelemetry.instrumentation.flask import FlaskInstrumentor

from apitally.shared import activation
from apitally.shared.activation import WSGIActivationShim
from apitally.shared.span_processor import get_server_span
from apitally.shared.startup import set_app_info
from apitally.shared.wsgi import BODY_TOO_LARGE, MAX_BODY_SIZE, WsgiTransportMiddleware, is_allowed_content_type


if TYPE_CHECKING:
    from _typeshed.wsgi import WSGIEnvironment
    from opentelemetry.sdk.trace import ReadableSpan


logger = logging.getLogger(__name__)

__all__ = ["init_apitally"]


def init_apitally(
    app: Flask,
    *,
    write_token: str | None = None,
    env: str | None = None,
    app_version: str | None = None,
    disabled: bool | None = None,
    capture_logs: bool | None = None,
    exclude_on_request: Callable[[ReadableSpan], bool] | None = None,
    exclude_on_response: Callable[[ReadableSpan], bool] | None = None,
    mask_request_body: Callable[[ReadableSpan, bytes], bytes | None] | None = None,
    mask_response_body: Callable[[ReadableSpan, bytes], bytes | None] | None = None,
    log_request_headers: bool | None = None,
    log_request_body: bool | None = None,
    log_response_headers: bool | None = None,
    log_response_body: bool | None = None,
    mask_query_params: list[str] | None = None,
    mask_headers: list[str] | None = None,
    mask_body_fields: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> None:
    """Set up Apitally for a Flask app; activation happens on the first request."""
    # Must be the first statement so locals() holds exactly the config kwargs
    config_kwargs = {k: v for k, v in locals().items() if k not in ("app", "app_version") and v is not None}
    try:
        activation.configure(**config_kwargs)
        if isinstance(app.wsgi_app, WSGIActivationShim):
            return
        transport = WsgiTransportMiddleware(app.wsgi_app, get_route=_create_route_resolver(app))
        app.wsgi_app = transport  # ty: ignore[invalid-assignment]
        if getattr(app, "_is_instrumented_by_opentelemetry", False):
            logger.debug("Flask app is already instrumented by OpenTelemetry, adapting")
        else:
            FlaskInstrumentor().instrument_app(app)
        app.after_request(_create_response_body_hook(transport))
        app.wsgi_app = WSGIActivationShim(app.wsgi_app)  # ty: ignore[invalid-assignment]
        set_app_info(framework="flask", paths=lambda: _get_paths(app), versions=_get_versions(app_version))
    except Exception:
        logger.exception("Error initializing Apitally for Flask")


def _create_response_body_hook(transport: WsgiTransportMiddleware) -> Callable[[Response], Response]:
    def capture_response_body(response: Response) -> Response:
        # The instrumentor's SERVER span ends in teardown_request, before the response
        # iterable reaches any WSGI layer, so the body is written here while the span
        # is still recording; direct_passthrough responses are not captured (design.md section 6)
        try:
            config = transport.refresh_config()
            if (
                config.log_response_body
                and not response.direct_passthrough
                and is_allowed_content_type(response.content_type)
                and (span := get_server_span()) is not None
                and span.is_recording()
            ):
                body = response.get_data()
                transport.set_body_attribute(
                    span,
                    "apitally.response.body",
                    body if len(body) <= MAX_BODY_SIZE else BODY_TOO_LARGE,
                    config.mask_response_body,
                    "mask_response_body",
                )
        except Exception:
            logger.exception("Error in Apitally after_request hook")
        return response

    return capture_response_body


def _create_route_resolver(app: Flask) -> Callable[[WSGIEnvironment], str | None]:
    def get_route(environ: WSGIEnvironment) -> str | None:
        # Resolved via the url_map because the request context is already gone when the
        # transport middleware finalizes the response iterable
        try:
            rule, _ = app.url_map.bind_to_environ(environ).match(return_rule=True)
            return rule.rule
        except Exception:
            return None

    return get_route


def _get_paths(app: Flask) -> list[dict[str, str]]:
    return [
        {"method": method, "path": rule.rule}
        for rule in app.url_map.iter_rules()
        if rule.methods is not None and rule.rule != "/static/<path:filename>"
        for method in rule.methods
        if method not in ("HEAD", "OPTIONS")
    ]


def _get_versions(app_version: str | None) -> dict[str, str]:
    versions: dict[str, str] = {}
    try:
        versions["flask"] = version("flask")
    except PackageNotFoundError:
        pass
    if app_version:
        versions["app"] = app_version
    return versions
