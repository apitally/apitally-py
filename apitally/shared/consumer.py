from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING

from opentelemetry.util.types import AttributeValue

from apitally.shared.span_processor import get_server_span


if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan


logger = logging.getLogger(__name__)

# Request-scoped consumer holder: the transport middleware (asgi.py/wsgi.py) calls
# reset_consumer_identifier() at request entry and resolve_consumer_identifier() at request
# completion, so consumer-dimension metrics stay complete even when a cooperative
# sampler drops the SERVER span
consumer_identifier_var: ContextVar[str | None] = ContextVar("apitally_consumer_identifier", default=None)


def set_consumer(identifier: str, name: str | None = None, group: str | None = None) -> None:
    try:
        identifier = str(identifier).strip()[:128]
        if not identifier:
            return
        consumer_identifier_var.set(identifier)
        span = get_server_span()
        if span is None or not span.is_recording():
            return
        span.set_attribute("apitally.consumer.identifier", identifier)
        if name and (name := str(name).strip()[:64]):
            span.set_attribute("apitally.consumer.name", name)
        if group and (group := str(group).strip()[:64]):
            span.set_attribute("apitally.consumer.group", group)
    except Exception:
        logger.debug("Error in set_consumer", exc_info=True)


def set_request_attribute(key: str, value: AttributeValue) -> None:
    try:
        span = get_server_span()
        if span is not None and span.is_recording():
            span.set_attribute(key, value)
    except Exception:
        logger.debug("Error in set_request_attribute", exc_info=True)


def capture_exception(exc: BaseException) -> None:
    try:
        span = get_server_span()
        if span is not None and span.is_recording():
            span.record_exception(exc)
    except Exception:
        logger.debug("Error in capture_exception", exc_info=True)


def get_consumer_identifier() -> str | None:
    return consumer_identifier_var.get()


def resolve_consumer_identifier(span: ReadableSpan | None) -> str | None:
    # Sync endpoints run in a copied context (anyio), where set_consumer's ContextVar write is
    # lost; the span object is shared across threads, so its attribute is the fallback
    identifier = consumer_identifier_var.get()
    if identifier is None and span is not None:
        value = (span.attributes or {}).get("apitally.consumer.identifier")
        identifier = value if isinstance(value, str) else None
    return identifier


def reset_consumer_identifier() -> None:
    consumer_identifier_var.set(None)
