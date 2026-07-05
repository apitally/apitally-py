from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from apitally.shared import activation, config, startup
from apitally.shared.asgi import ApitallyASGIMiddleware


if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan


__all__ = ["init_apitally"]

logger = logging.getLogger(__name__)


def init_apitally(
    app: FastAPI,
    *,
    write_token: str | None = None,
    env: str | None = None,
    app_version: str | None = None,
    openapi_url: str | None = "/openapi.json",
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
    """Set up Apitally for a FastAPI application. Errors never propagate (design.md section 9)."""
    try:
        activation.configure(**config.explicit_kwargs(locals()))
        _instrument_app(app)
        startup.set_app_info(
            framework="fastapi",
            paths=lambda: _get_paths(app),
            versions=startup.resolve_versions(app_version, fastapi="fastapi", starlette="starlette"),
            openapi=lambda: _get_openapi(app, openapi_url),
        )
    except Exception:
        logger.exception("Apitally setup for FastAPI failed")


def _instrument_app(app: FastAPI) -> None:
    if any(m.cls is ApitallyASGIMiddleware for m in app.user_middleware):
        return
    if not getattr(app, "_is_instrumented_by_opentelemetry", False):
        FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])
    # Lands inside the SERVER span regardless of order: the instrumentor wraps the whole built stack lazily
    app.add_middleware(ApitallyASGIMiddleware, resolve_route=_resolve_route)
    # Chain-patch after the instrumentor's patch so the shim wraps the built stack outermost
    # (add_middleware cannot reach outside the instrumentor's wrap; design.md section 7)
    build_inner = app.build_middleware_stack

    def build_with_shim() -> Any:
        return activation.ASGIActivationShim(build_inner())

    app.build_middleware_stack = build_with_shim  # ty: ignore[invalid-assignment]


def _resolve_route(scope: dict[str, Any]) -> str | None:
    # FastAPI 0.138+ keeps included-router routes unflattened; only the effective route
    # context carries the full templated path, scope["route"].path lacks the prefix (0.x parity)
    context = scope.get("fastapi", {}).get("effective_route_context")
    path = getattr(context, "path", None) or getattr(scope.get("route"), "path", None)
    return path if isinstance(path, str) else None


def _get_paths(app: FastAPI) -> list[dict[str, str]]:
    paths = []
    for route in _iter_routes(app.routes):
        path = getattr(route, "path", None)
        if not isinstance(path, str) or not getattr(route, "include_in_schema", True):
            continue
        for method in sorted(getattr(route, "methods", None) or []):
            if method in ("HEAD", "OPTIONS"):
                continue
            entry = {"method": method, "path": path}
            if summary := getattr(route, "summary", None):
                entry["summary"] = summary
            if description := getattr(route, "description", None):
                entry["description"] = description
            paths.append(entry)
    return paths


def _iter_routes(routes: list[Any]) -> Iterator[Any]:
    # Expand FastAPI 0.138+ included-router nodes into their effective route contexts,
    # which expose path, methods, summary, and description with the full templated path
    for route in routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if callable(contexts):
            yield from contexts()
        else:
            yield route


def _get_openapi(app: FastAPI, openapi_url: str | None) -> str | None:
    # 0.x semantics: emit only when the app actually serves a schema at openapi_url
    if not openapi_url or not any(getattr(route, "path", None) == openapi_url for route in app.routes):
        return None
    return json.dumps(app.openapi())
