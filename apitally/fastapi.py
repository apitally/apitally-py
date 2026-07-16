from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from apitally.shared import activation, config, startup
from apitally.shared.asgi import ApitallyASGIMiddleware


if TYPE_CHECKING:
    from starlette.routing import BaseRoute
    from starlette.types import Scope


__all__ = ["init"]

logger = logging.getLogger(__name__)


def init(
    app: FastAPI,
    *,
    app_version: str | None = None,
    openapi_url: str | None = "/openapi.json",
    **kwargs: Any,
) -> None:
    """
    Set up Apitally for a FastAPI application.

    For more information, see:
    - Setup guide: https://docs.apitally.io/setup-guides/fastapi
    - Reference: https://docs.apitally.io/sdk-reference/python
    """
    try:
        cfg = activation.configure(**config.explicit_kwargs(kwargs))
        if cfg.disabled:
            return
        _instrument_app(app)
        startup.set_app_info(
            framework="fastapi",
            paths=lambda: _get_paths(app),
            versions=startup.resolve_versions(app_version, fastapi="fastapi", starlette="starlette"),
            openapi=lambda: _get_openapi(app, openapi_url),
        )
    except Exception:  # pragma: no cover
        logger.exception("Apitally setup for FastAPI failed")


def _instrument_app(app: FastAPI) -> None:
    if getattr(app, "_is_instrumented_by_apitally", False):
        return
    setattr(app, "_is_instrumented_by_apitally", True)
    if not getattr(app, "_is_instrumented_by_opentelemetry", False):
        FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])

    # The instrumentor already replaced build_middleware_stack; replace it again on top so the
    # transport middleware wraps the whole instrumented stack, outside ServerErrorMiddleware:
    # responses to unhandled exceptions then pass through it, with the SERVER span still recording
    build_inner = app.build_middleware_stack

    def build_with_shim() -> activation.ASGIActivationShim:
        return activation.ASGIActivationShim(
            ApitallyASGIMiddleware(
                build_inner(),  # ty: ignore[invalid-argument-type]
                resolve_route=_resolve_route,
            )
        )

    app.build_middleware_stack = build_with_shim  # ty: ignore[invalid-assignment]


def _resolve_route(scope: Scope) -> str | None:
    # FastAPI 0.138+ no longer copies included-router routes into the app's route list; the full
    # templated path is only on the effective route context, scope["route"].path lacks the prefix
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


def _iter_routes(routes: list[BaseRoute]) -> Iterator[Any]:
    # FastAPI 0.138+ route lists contain included-router entries; effective_route_contexts()
    # expands them into route objects whose path, methods, summary, and description use the
    # full templated path
    for route in routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if callable(contexts):
            yield from contexts()
        else:
            yield route


def _get_openapi(app: FastAPI, openapi_url: str | None) -> str | None:
    # Emit only when the app actually serves a schema at openapi_url
    if not openapi_url or not any(getattr(route, "path", None) == openapi_url for route in app.routes):
        return None
    return json.dumps(app.openapi())
