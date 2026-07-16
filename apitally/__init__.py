from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from apitally.otel import instrument, span
from apitally.shared.consumer import set_consumer
from apitally.shared.helpers import capture_exception, set_request_attribute


if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan


__all__ = [
    "capture_exception",
    "init",
    "instrument",
    "set_consumer",
    "set_request_attribute",
    "span",
]

_FRAMEWORK_PACKAGES = frozenset({"blacksheep", "django", "fastapi", "flask", "litestar", "starlette"})

# Default for framework-specific params, which are only forwarded when explicitly set
_UNSET: Any = object()


def init(
    app: Any = None,
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
    openapi_url: str | None = _UNSET,
    urlconf: str | list[str | None] | None = _UNSET,
    include_django_views: bool = _UNSET,
) -> None:
    """
    Set up Apitally for an application.

    For Django, call this without an app argument at the end of settings.py.
    For Litestar, pass apitally.litestar.ApitallyPlugin to the Litestar constructor instead.

    For more information, see:
    - Setup guides: https://docs.apitally.io/setup-guides
    - Reference: https://docs.apitally.io/sdk-reference/python
    """
    kwargs = locals()
    app = kwargs.pop("app")
    kwargs = {name: value for name, value in kwargs.items() if value is not _UNSET}

    if app is not None:
        framework = _detect_framework_package(app)
    elif "django.conf" in sys.modules:
        framework = "django"
    else:
        raise TypeError("apitally.init requires an app argument for all frameworks except Django")
    if framework is None:
        raise TypeError(
            f"apitally.init could not detect a supported framework from the app of type "
            f"{type(app).__qualname__}; use the framework-specific init function instead, "
            f"e.g. apitally.fastapi.init"
        )
    if framework == "litestar":
        raise TypeError(
            "Litestar apps must be set up at construction time; pass "
            "apitally.litestar.ApitallyPlugin(...) to the Litestar constructor instead"
        )
    if framework == "django" and app is not None:
        raise TypeError("For Django, call apitally.init() without an app argument at the end of settings.py")

    module = importlib.import_module(f"apitally.{framework}")
    if app is None:
        module.init(**kwargs)
    else:
        module.init(app, **kwargs)


def _detect_framework_package(app: Any) -> str | None:
    for cls in type(app).__mro__:
        package = cls.__module__.partition(".")[0]
        if package in _FRAMEWORK_PACKAGES:
            return package
    # Middleware wrappers like OpenTelemetryMiddleware hold the wrapped app in an "app" attribute
    inner = getattr(app, "app", None)
    return _detect_framework_package(inner) if inner is not None else None
