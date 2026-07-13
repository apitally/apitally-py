from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apitally.shared.context import get_server_span


if TYPE_CHECKING:
    from sentry_sdk.types import Event, Hint


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


def sentry_event_processor(event: Event, hint: Hint) -> Event:
    try:
        if "exception" in event and (event_id := event.get("event_id")):
            span = get_server_span()
            if span is not None and span.is_recording():
                span.set_attribute("apitally.exception.sentry_event_id", event_id)
    except Exception:  # pragma: no cover
        logger.debug("Error in Apitally Sentry event processor", exc_info=True)
    return event
