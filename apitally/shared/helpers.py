import logging

from opentelemetry.util.types import AttributeValue

from apitally.shared.context import get_server_span


logger = logging.getLogger(__name__)


def set_request_attribute(key: str, value: AttributeValue) -> None:
    try:
        span = get_server_span()
        if span is not None and span.is_recording():
            span.set_attribute(key, value)
    except Exception:  # pragma: no cover
        logger.debug("Error in set_request_attribute", exc_info=True)


def capture_exception(exc: BaseException) -> None:
    try:
        span = get_server_span()
        if span is not None and span.is_recording():
            span.record_exception(exc)
    except Exception:  # pragma: no cover
        logger.debug("Error in capture_exception", exc_info=True)
