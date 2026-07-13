import logging
from contextvars import ContextVar
from dataclasses import dataclass

from opentelemetry.sdk.trace import Span

from apitally.shared.span_processor import get_server_span


logger = logging.getLogger(__name__)


@dataclass(slots=True, eq=False)
class ConsumerHolder:
    identifier: str | None = None
    name: str | None = None
    group: str | None = None
    # True for holders installed by the transport middleware at request entry
    owned: bool = False


# Request-scoped mutable holder, installed by the transport middleware at request entry and
# cleared at completion. Copied contexts (threadpool endpoints, BaseHTTPMiddleware child tasks)
# share the holder by reference, so set_consumer's mutation is visible at completion.
consumer_holder_var: ContextVar[ConsumerHolder | None] = ContextVar("apitally_consumer_holder", default=None)


def set_consumer(identifier: str, name: str | None = None, group: str | None = None) -> None:
    try:
        identifier = str(identifier).strip()[:128]
        if not identifier:  # pragma: no cover
            return
        name = (str(name).strip()[:64] or None) if name else None
        group = (str(group).strip()[:64] or None) if group else None
        holder = consumer_holder_var.get()
        if holder is None or not holder.owned:
            # An unowned holder may be shared across requests via a copied base context; never mutate it
            holder = ConsumerHolder(identifier, name, group)
            consumer_holder_var.set(holder)
        else:
            holder.identifier = identifier
            holder.name = name
            holder.group = group
        span = get_server_span()
        if span is not None and span.is_recording():
            write_consumer_span_attributes(span, holder)
    except Exception:  # pragma: no cover
        logger.debug("Error in set_consumer", exc_info=True)


def get_consumer_identifier() -> str | None:
    holder = consumer_holder_var.get()
    return holder.identifier if holder is not None else None


def init_consumer() -> None:
    """Install the request's holder at transport middleware entry, adopting a consumer set earlier."""
    holder = ConsumerHolder(owned=True)
    prev = consumer_holder_var.get()
    if prev is not None and not prev.owned:
        holder.identifier, holder.name, holder.group = prev.identifier, prev.name, prev.group
        prev.identifier = prev.name = prev.group = None  # a later request must not adopt these again
    consumer_holder_var.set(holder)


def reset_consumer() -> None:
    consumer_holder_var.set(None)


def write_consumer_span_attributes(span: Span, holder: ConsumerHolder) -> None:
    if holder.identifier:
        span.set_attribute("apitally.consumer.identifier", holder.identifier)
    if holder.name:
        span.set_attribute("apitally.consumer.name", holder.name)
    if holder.group:
        span.set_attribute("apitally.consumer.group", holder.group)
