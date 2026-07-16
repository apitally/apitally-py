from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from flask import Flask
from opentelemetry.instrumentation.flask import FlaskInstrumentor

from apitally.shared import activation, config, startup
from apitally.shared.wsgi import ApitallyWSGIMiddleware


if TYPE_CHECKING:
    from _typeshed.wsgi import WSGIEnvironment
    from opentelemetry.sdk.trace import ReadableSpan


logger = logging.getLogger(__name__)

__all__ = ["init"]


def init(
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
    """
    Set up Apitally for a Flask app.

    For more information, see:
    - Setup guide: https://docs.apitally.io/setup-guides/flask
    - Reference: https://docs.apitally.io/sdk-reference/python
    """
    try:
        cfg = activation.configure(**config.explicit_kwargs(locals()))
        if cfg.disabled:
            return
        if isinstance(app.wsgi_app, activation.WSGIActivationShim):
            return
        transport = ApitallyWSGIMiddleware(app.wsgi_app, get_route=_create_route_resolver(app))
        app.wsgi_app = transport  # ty: ignore[invalid-assignment]
        if not getattr(app, "_is_instrumented_by_opentelemetry", False):
            FlaskInstrumentor().instrument_app(app)
        app.wsgi_app = activation.WSGIActivationShim(app.wsgi_app)  # ty: ignore[invalid-assignment]
        startup.set_app_info(
            framework="flask",
            paths=lambda: _get_paths(app),
            versions=startup.resolve_versions(app_version, flask="flask"),
        )
    except Exception:  # pragma: no cover
        logger.exception("Error initializing Apitally for Flask")


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
