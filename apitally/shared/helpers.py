import logging

from opentelemetry.util.types import AttributeValue

from apitally.shared.context import get_server_span
from apitally.shared.server_errors import set_exception


logger = logging.getLogger(__name__)

CUSTOM_ATTRIBUTE_KEYS_ATTRIBUTE = "apitally.request.custom_attribute_keys"


def set_request_attribute(key: str, value: AttributeValue) -> None:
    try:
        span = get_server_span()
        if span is not None and span.is_recording():
            span.set_attribute(key, value)
            # The OTel SDK drops invalid keys and values, so only list keys that were stored
            attributes = span.attributes or {}
            keys = attributes.get(CUSTOM_ATTRIBUTE_KEYS_ATTRIBUTE, ())
            if key in attributes and isinstance(keys, tuple) and key not in keys:
                span.set_attribute(CUSTOM_ATTRIBUTE_KEYS_ATTRIBUTE, (*keys, key))
    except Exception:  # pragma: no cover
        logger.debug("Error in set_request_attribute", exc_info=True)


def capture_exception(exc: BaseException) -> None:
    """Capture the first exception for the current request."""
    try:
        if isinstance(exc, Exception):
            set_exception(exc)
        span = get_server_span()
        if span is not None and span.is_recording():
            span.record_exception(exc)
    except Exception:  # pragma: no cover
        logger.debug("Error in capture_exception", exc_info=True)
