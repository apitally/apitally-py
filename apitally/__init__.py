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
    capture_logs: bool = True,
    capture_request_headers: bool = False,
    capture_request_body: bool = False,
    capture_response_headers: bool = True,
    capture_response_body: bool = False,
    mask_query_params: list[str] | None = None,
    mask_headers: list[str] | None = None,
    mask_body_fields: list[str] | None = None,
    mask_request_body: Callable[[ReadableSpan, bytes], bytes | None] | None = None,
    mask_response_body: Callable[[ReadableSpan, bytes], bytes | None] | None = None,
    exclude_paths: list[str] | None = None,
    sample_rate: float = 1.0,
    sample_on_request: Callable[[ReadableSpan], float | bool | None] | None = None,
    sample_on_response: Callable[[ReadableSpan], float | bool | None] | None = None,
    urlconf: str | list[str | None] | None = _UNSET,
    include_django_views: bool = _UNSET,
) -> None:
    """
    Set up Apitally for an application.

    Args:
        app: Application to instrument. Omit this only for Django.
        write_token: The Apitally client write token. When omitted, the `APITALLY_WRITE_TOKEN`
            environment variable is used.
        env: The environment name reported to Apitally. When omitted, the `APITALLY_ENV`
            environment variable is used, falling back to `"prod"`.
        app_version: The application version reported to Apitally.
        disabled: Whether to disable Apitally. When omitted, `APITALLY_DISABLED` and
            `OTEL_SDK_DISABLED` are respected.
        capture_logs: Whether to capture application logs from the standard `logging` module
            and correlate them with requests.
        capture_request_headers: Whether to capture request headers. Sensitive values are
            redacted before export.
        capture_request_body: Whether to capture eligible request bodies up to 50 KB. Sensitive
            JSON fields are redacted before export.
        capture_response_headers: Whether to capture response headers. Sensitive values are
            redacted before export.
        capture_response_body: Whether to capture eligible response bodies up to 50 KB.
            Sensitive JSON fields are redacted before export.
        mask_query_params: Additional case-insensitive regular expressions for query parameter
            names whose values should be redacted.
        mask_headers: Additional case-insensitive regular expressions for header names whose
            values should be redacted.
        mask_body_fields: Additional case-insensitive regular expressions for JSON body field
            names whose string values should be redacted.
        mask_request_body: A callback that receives the ended request SERVER span and captured
            request body as bytes. It must return the body to export as bytes, or `None` to
            replace the entire body with `[REDACTED]`.
        mask_response_body: A callback that receives the ended request SERVER span and captured
            response body as bytes. It must return the body to export as bytes, or `None` to
            replace the entire body with `[REDACTED]`.
        exclude_paths: Additional case-insensitive regular expressions for request paths to
            exclude from traces and logs. Excluded requests are still included in metrics.
        sample_rate: The fraction of requests to capture as traces and logs, from `0.0` to `1.0`.
            Metrics are not sampled.
        sample_on_request: A callback that receives the request SERVER span at request start and
            returns a capture probability, a boolean, or `None` to use `sample_rate`.
        sample_on_response: A callback that receives the ended request SERVER span and returns a
            capture probability, a boolean, or `None` to preserve the request-stage decision. It
            cannot retain a request that was already sampled out.
        urlconf: For Django, the URLconf module or modules used to discover routes and schemas.
            `None` uses the root URLconf.
        include_django_views: For Django, whether to include class-based Django views in the
            reported endpoint list in addition to Django REST Framework and Django Ninja routes.

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
