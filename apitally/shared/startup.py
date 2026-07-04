from __future__ import annotations

import json
import logging
import platform
import time
from collections.abc import Callable
from typing import Any

from opentelemetry.trace import INVALID_SPAN, set_span_in_context

from apitally.shared import activation


logger = logging.getLogger(__name__)

EVENT_NAME = "apitally.app.startup"
MAX_OPENAPI_BYTES = 4_000_000

app_info: dict[str, Any] = {}


def set_app_info(
    framework: str,
    paths: list[dict[str, str]] | Callable[[], list[dict[str, str]]] | None = None,
    versions: dict[str, str] | Callable[[], dict[str, str]] | None = None,
    openapi: str | Callable[[], str | None] | None = None,
) -> None:
    """Called by framework adapters at configure time; values may be zero-arg callables resolved at emit time."""
    app_info.update(framework=framework, paths=paths, versions=versions, openapi=openapi)
    if emit_startup_event not in activation.on_activate_hooks:
        activation.register_on_activate_hook(emit_startup_event)


def emit_startup_event() -> None:
    """Emit the spec section 9 startup event directly on the private LoggerProvider."""
    if activation.logger_provider is None:
        return
    payload: dict[str, Any] = {
        "framework": app_info.get("framework"),
        "versions": {"python": platform.python_version(), **(resolve(app_info.get("versions")) or {})},
    }
    if (paths := resolve(app_info.get("paths"))) is not None:
        payload["paths"] = paths
    openapi = resolve(app_info.get("openapi"))
    if openapi and len(openapi.encode()) <= MAX_OPENAPI_BYTES:
        payload["openapi"] = openapi
    # The explicit invalid-span context keeps trace context off the record (spec section 9)
    activation.logger_provider.get_logger("apitally").emit(
        timestamp=time.time_ns(),
        context=set_span_in_context(INVALID_SPAN),
        body=json.dumps(payload, separators=(",", ":")),
        event_name=EVENT_NAME,
    )


def resolve(value: Any) -> Any:
    if not callable(value):
        return value
    try:
        return value()
    except Exception:
        logger.exception("Error resolving Apitally app info for the startup event")
        return None


def reset() -> None:
    app_info.clear()
