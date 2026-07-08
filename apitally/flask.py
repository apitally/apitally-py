from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from flask import Flask, Response
from opentelemetry.instrumentation.flask import FlaskInstrumentor

from apitally.shared import activation, config, startup
from apitally.shared.capture import BODY_TOO_LARGE, MAX_BODY_SIZE, is_allowed_content_type
from apitally.shared.span_processor import get_server_span, is_server_span_kept
from apitally.shared.wsgi import ApitallyWSGIMiddleware


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
    """Set up Apitally for a Flask app. Activation happens on the first request."""
    try:
        activation.configure(**config.explicit_kwargs(locals()))
        if isinstance(app.wsgi_app, activation.WSGIActivationShim):
            return
        transport = ApitallyWSGIMiddleware(
            app.wsgi_app,
            get_route=_create_route_resolver(app),
            capture_response_body=False,
        )
        app.wsgi_app = transport  # ty: ignore[invalid-assignment]
        if not getattr(app, "_is_instrumented_by_opentelemetry", False):
            FlaskInstrumentor().instrument_app(app)
        app.after_request(_create_response_body_hook(transport))
        app.wsgi_app = activation.WSGIActivationShim(app.wsgi_app)  # ty: ignore[invalid-assignment]
        startup.set_app_info(
            framework="flask",
            paths=lambda: _get_paths(app),
            versions=startup.resolve_versions(app_version, flask="flask"),
        )
    except Exception:  # pragma: no cover
        logger.exception("Error initializing Apitally for Flask")


def _create_response_body_hook(transport: ApitallyWSGIMiddleware) -> Callable[[Response], Response]:
    def capture_response_body(response: Response) -> Response:
        # The instrumentor's SERVER span ends in teardown_request, before the response
        # iterable reaches any WSGI layer, so the body is written here while the span
        # is still recording; streaming responses are not captured
        try:
            config = transport.config
            if (
                is_server_span_kept()
                and config.log_response_body
                and not response.direct_passthrough
                and not response.is_streamed
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
        except Exception:  # pragma: no cover
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
        except Exception:  # pragma: no cover
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
