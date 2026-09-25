import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from itertools import islice
from typing import Any

from opentelemetry._logs import Logger
from opentelemetry.sdk.trace import Span
from opentelemetry.trace import INVALID_SPAN, set_span_in_context

from apitally.shared.context import get_server_span


logger = logging.getLogger(__name__)

EVENT_NAME = "apitally.consumer.update"
MAX_CACHED_CONSUMERS = 10_000
MAX_ATTRIBUTES_PER_UPDATE = 10


@dataclass(slots=True, eq=False)
class ConsumerHolder:
    identifier: str | None = None
    name: str | None = None
    group: str | None = None
    attributes: dict[str, str | None] = field(default_factory=dict)
    # True for holders installed by the transport middleware at request entry
    owned: bool = False


# Request-scoped mutable holder, installed by the transport middleware at request entry and
# cleared at completion. Copied contexts (threadpool endpoints, BaseHTTPMiddleware child tasks)
# share the holder by reference, so set_consumer's mutation is visible at completion.
consumer_holder_var: ContextVar[ConsumerHolder | None] = ContextVar("apitally_consumer_holder", default=None)

# Set by activation once the logs pipeline exists
update_logger: Logger | None = None
# Hash of the last emitted update per consumer identifier, in least recently used order
update_hashes: OrderedDict[str, int] = OrderedDict()
update_hashes_lock = threading.Lock()


def set_consumer(
    identifier: str,
    name: str | None = None,
    group: str | None = None,
    attributes: Mapping[str, str | int | float | bool | None] | None = None,
) -> None:
    """Identify the consumer of the current request. Repeated calls for the same identifier
    merge: attributes are a partial update where None deletes a key, and scalar values are
    converted to strings."""
    try:
        identifier = str(identifier).strip()[:128]
        if not identifier:  # pragma: no cover
            return
        name = (str(name).strip()[:64] or None) if name else None
        group = (str(group).strip()[:64] or None) if group else None
        holder = consumer_holder_var.get()
        if holder is None or not holder.owned:
            # An unowned holder may be shared across requests via a copied base context; never mutate it
            holder = ConsumerHolder() if holder is None else replace(holder, attributes=dict(holder.attributes))
            consumer_holder_var.set(holder)
        if holder.identifier != identifier:
            holder.identifier, holder.name, holder.group, holder.attributes = identifier, None, None, {}
        holder.name = name or holder.name
        holder.group = group or holder.group
        for key, value in (attributes or {}).items():
            key = str(key).strip()
            match value:
                case int() | float() | bool():
                    value = str(value).lower()
                case str() | None:
                    pass
                case _:
                    continue
            value = (value.strip() or None) if value else None
            if 0 < len(key) <= 64 and (value is None or len(value) <= 1024):
                holder.attributes[key] = value
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
        holder = replace(prev, owned=True)
        # A later request must not adopt these again
        prev.identifier, prev.name, prev.group, prev.attributes = None, None, None, {}
    consumer_holder_var.set(holder)


def reset_consumer() -> None:
    consumer_holder_var.set(None)


def emit_consumer_update_if_changed() -> None:
    """Emit the request's consumer profile unless it matches the last update emitted for this consumer."""
    holder = consumer_holder_var.get()
    if update_logger is None or holder is None or not holder.identifier:
        return
    attributes = dict(islice(holder.attributes.items(), MAX_ATTRIBUTES_PER_UPDATE))
    if not (holder.name or holder.group or attributes):
        return
    payload_hash = hash((holder.identifier, holder.name, holder.group, frozenset(attributes.items())))
    with update_hashes_lock:
        changed = update_hashes.get(holder.identifier) != payload_hash
        update_hashes[holder.identifier] = payload_hash
        update_hashes.move_to_end(holder.identifier)
        if len(update_hashes) > MAX_CACHED_CONSUMERS:
            update_hashes.popitem(last=False)
    if not changed:
        return
    body: dict[str, Any] = {"identifier": holder.identifier}
    if holder.name:
        body["name"] = holder.name
    if holder.group:
        body["group"] = holder.group
    if attributes:
        body["attributes"] = attributes
    # The explicit invalid-span context keeps trace context off the record
    update_logger.emit(
        timestamp=time.time_ns(),
        context=set_span_in_context(INVALID_SPAN),
        body=body,
        event_name=EVENT_NAME,
    )


def write_consumer_span_attributes(span: Span, holder: ConsumerHolder) -> None:
    if holder.identifier:
        span.set_attribute("apitally.consumer.identifier", holder.identifier)


def reset() -> None:
    global update_logger, update_hashes_lock
    update_logger = None
    update_hashes_lock = threading.Lock()
    update_hashes.clear()
