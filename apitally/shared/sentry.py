from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apitally.shared.context import get_server_span, is_server_span_kept


if TYPE_CHECKING:
    from sentry_sdk.types import Event, Hint


SENTRY_EVENT_ID_ATTRIBUTE = "apitally.exception.sentry_event_id"
MAX_PENDING_EVENT_IDS = 2_048

logger = logging.getLogger(__name__)
installed = False

# Sentry may capture an unhandled exception after the SERVER span has ended and been queued for
# export; event IDs are held here by span ID until ApitallySpanExporter merges them at export time.
pending_event_ids: dict[int, str] = {}


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
            if span is not None and span.context is not None and is_server_span_kept():
                if len(pending_event_ids) >= MAX_PENDING_EVENT_IDS:  # pragma: no cover
                    pending_event_ids.pop(next(iter(pending_event_ids)), None)
                    logger.debug("Pending Sentry event ID cap reached, dropping oldest entry")
                pending_event_ids[span.context.span_id] = event_id
    except Exception:  # pragma: no cover
        logger.debug("Error in Sentry event processor", exc_info=True)
    return event


def pop_sentry_event_id(span_id: int) -> str | None:
    """Called by ApitallySpanExporter on the export thread when a span is exported."""
    return pending_event_ids.pop(span_id, None)
