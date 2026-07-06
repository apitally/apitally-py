from __future__ import annotations

import logging
from typing import Any

from apitally.shared.span_processor import get_server_span


logger = logging.getLogger(__name__)

installed = False


def install() -> None:
    """Install a global Sentry event processor once, if sentry-sdk is importable."""
    global installed
    if installed:
        return
    try:
        from sentry_sdk.scope import add_global_event_processor
    except ImportError:
        return
    add_global_event_processor(sentry_event_processor)
    installed = True


def sentry_event_processor(event: Any, hint: Any) -> Any:
    try:
        if "exception" in event and (event_id := event.get("event_id")):
            span = get_server_span()
            if span is not None and span.is_recording():
                span.set_attribute("apitally.exception.sentry_event_id", event_id)
    except Exception:
        logger.debug("Error in Apitally Sentry event processor", exc_info=True)
    return event
