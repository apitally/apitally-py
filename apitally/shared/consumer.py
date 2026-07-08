import logging
from contextvars import ContextVar
from dataclasses import dataclass

from apitally.shared.span_processor import get_server_span


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConsumerHolder:
    identifier: str | None = None


# Request-scoped mutable holder, set by the transport middleware at request entry. Copied
# contexts (threadpool endpoints, BaseHTTPMiddleware child tasks) share the holder by
# reference, so set_consumer's mutation is visible to the middleware at request completion.
consumer_holder_var: ContextVar[ConsumerHolder | None] = ContextVar("apitally_consumer_holder", default=None)


def set_consumer(identifier: str, name: str | None = None, group: str | None = None) -> None:
    try:
        identifier = str(identifier).strip()[:128]
        if not identifier:  # pragma: no cover
            return
        holder = consumer_holder_var.get()
        if holder is None:
            holder = ConsumerHolder()
            consumer_holder_var.set(holder)
        holder.identifier = identifier
        span = get_server_span()
        if span is None or not span.is_recording():
            return
        span.set_attribute("apitally.consumer.identifier", identifier)
        if name and (name := str(name).strip()[:64]):
            span.set_attribute("apitally.consumer.name", name)
        if group and (group := str(group).strip()[:64]):
            span.set_attribute("apitally.consumer.group", group)
    except Exception:  # pragma: no cover
        logger.debug("Error in set_consumer", exc_info=True)


def get_consumer_identifier() -> str | None:
    holder = consumer_holder_var.get()
    return holder.identifier if holder is not None else None


def reset_consumer() -> None:
    consumer_holder_var.set(ConsumerHolder())
