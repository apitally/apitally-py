from __future__ import annotations

import sys
import threading
import traceback
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


if sys.version_info >= (3, 11):
    exception_group_types: tuple[type[BaseExceptionGroup], ...] = (BaseExceptionGroup,)  # noqa: F821
else:  # pragma: no cover
    try:
        from exceptiongroup import BaseExceptionGroup as BackportBaseExceptionGroup

        exception_group_types = (BackportBaseExceptionGroup,)
    except ImportError:  # pragma: no cover
        exception_group_types = ()

EVENT_NAME = "apitally.request.server_error"
MAX_ERRORS = 100
MAX_EXCEPTION_TYPE_LENGTH = 256
MAX_EXCEPTION_MESSAGE_LENGTH = 2_048
MAX_STACKTRACE_LENGTH = 65_536
MESSAGE_TRUNCATION_SUFFIX = "... (truncated)"
STACKTRACE_TRUNCATION_PREFIX = "... (truncated) ...\n"


@dataclass(slots=True, eq=False)
class ExceptionHolder:
    exception: BaseException | None = None
    sentry_event_id: str | None = None
    sentry_event_exception: BaseException | None = None
    # Allows outer Sentry middleware to enrich an aggregate after request finalization.
    server_error_key: ServerErrorKey | None = None


@dataclass(frozen=True, slots=True)
class ServerErrorKey:
    method: str
    path: str
    type: str
    message: str
    stacktrace_fingerprint: tuple[Any, ...]


@dataclass(slots=True)
class ServerErrorAggregate:
    stacktrace: str
    counts: dict[str | None, int] = field(default_factory=dict)
    sentry_event_id: str | None = None


exception_holder_var: ContextVar[ExceptionHolder | None] = ContextVar("apitally_exception_holder", default=None)
server_error_lock = threading.Lock()
server_error_aggregates: dict[ServerErrorKey, ServerErrorAggregate] = {}


def init_exception_holder() -> ExceptionHolder:
    holder = ExceptionHolder()
    exception_holder_var.set(holder)
    return holder


def reset_exception_holder() -> None:
    exception_holder_var.set(None)


def set_exception(exception: BaseException, holder: ExceptionHolder | None = None) -> None:
    holder = holder or exception_holder_var.get()
    if holder is None or holder.exception is not None:
        return
    exception = collapse_exception_group(exception)
    if holder.sentry_event_exception is not None and holder.sentry_event_exception is not exception:
        holder.sentry_event_id = None
        holder.sentry_event_exception = None
        holder.server_error_key = None
    holder.exception = exception


def set_sentry_event_id(event_id: str, exception: BaseException | None = None) -> None:
    holder = exception_holder_var.get()
    if holder is None:
        return
    if exception is not None:
        exception = collapse_exception_group(exception)
        if holder.exception is not None and holder.exception is not exception:
            return
        holder.sentry_event_exception = exception
    holder.sentry_event_id = event_id
    if holder.server_error_key is not None:
        with server_error_lock:
            aggregate = server_error_aggregates.get(holder.server_error_key)
            if aggregate is not None:
                aggregate.sentry_event_id = event_id


def collapse_exception_group(exception: BaseException) -> BaseException:
    while isinstance(exception, exception_group_types) and len(exception.exceptions) == 1:  # ty: ignore[unresolved-attribute]
        exception = exception.exceptions[0]  # ty: ignore[unresolved-attribute]
    return exception


def add_server_error(
    consumer: str | None,
    method: str,
    path: str | None,
    exception_holder: ExceptionHolder,
) -> None:
    if exception_holder.exception is None:
        return
    method = method.upper()
    if method == "OPTIONS" or not path:
        return
    exception = exception_holder.exception
    key = ServerErrorKey(
        method=method,
        path=path,
        type=format_exception_type(exception),
        message=format_exception_message(exception),
        stacktrace_fingerprint=get_stacktrace_fingerprint(exception),
    )
    with server_error_lock:
        aggregate = server_error_aggregates.get(key)
        if aggregate is None:
            if len(server_error_aggregates) >= MAX_ERRORS:
                return
            # Formatting is expensive, so it only happens once per error
            aggregate = server_error_aggregates[key] = ServerErrorAggregate(
                stacktrace=format_exception_stacktrace(exception)
            )
        aggregate.counts[consumer] = aggregate.counts.get(consumer, 0) + 1
        aggregate.sentry_event_id = exception_holder.sentry_event_id or aggregate.sentry_event_id
        exception_holder.server_error_key = key


def drain_server_errors() -> list[dict[str, Any]]:
    global server_error_aggregates
    with server_error_lock:
        aggregates = server_error_aggregates
        server_error_aggregates = {}
    events = []
    for error, aggregate in aggregates.items():
        body: dict[str, Any] = {
            "method": error.method,
            "path": error.path,
            "type": error.type,
            "message": error.message,
            "stacktrace": aggregate.stacktrace,
            "counts": [
                {"count": count} if consumer is None else {"consumer": consumer, "count": count}
                for consumer, count in aggregate.counts.items()
            ],
        }
        if aggregate.sentry_event_id is not None:
            body["sentry_event_id"] = aggregate.sentry_event_id
        events.append(body)
    return events


def reset() -> None:
    global server_error_aggregates, server_error_lock
    server_error_aggregates = {}
    server_error_lock = threading.Lock()
    reset_exception_holder()


def format_exception_type(exception: BaseException) -> str:
    exception_type = type(exception)
    return f"{exception_type.__module__}.{exception_type.__qualname__}"[:MAX_EXCEPTION_TYPE_LENGTH]


def format_exception_message(exception: BaseException) -> str:
    message = str(exception).strip()
    if len(message) <= MAX_EXCEPTION_MESSAGE_LENGTH:
        return message
    cutoff = MAX_EXCEPTION_MESSAGE_LENGTH - len(MESSAGE_TRUNCATION_SUFFIX)
    return message[:cutoff] + MESSAGE_TRUNCATION_SUFFIX


def get_stacktrace_fingerprint(exception: BaseException | None) -> tuple[Any, ...]:
    # Follows the same exception chain as traceback.format_exception
    fingerprint: list[Any] = []
    seen: set[int] = set()
    while exception is not None and id(exception) not in seen:
        seen.add(id(exception))
        fingerprint.append(type(exception))
        traceback_entry = exception.__traceback__
        while traceback_entry is not None:
            code = traceback_entry.tb_frame.f_code
            fingerprint.append((code.co_filename, code.co_name, traceback_entry.tb_lineno))
            traceback_entry = traceback_entry.tb_next
        exception = exception.__cause__ or (None if exception.__suppress_context__ else exception.__context__)
    return tuple(fingerprint)


def format_exception_stacktrace(exception: BaseException) -> str:
    traceback_lines = traceback.format_exception(exception)
    if sum(map(len, traceback_lines)) <= MAX_STACKTRACE_LENGTH:
        return "".join(traceback_lines).strip()
    cutoff = MAX_STACKTRACE_LENGTH - len(STACKTRACE_TRUNCATION_PREFIX)
    lines = []
    length = 0
    for line in reversed(traceback_lines):
        if length + len(line) > cutoff:
            lines.append(STACKTRACE_TRUNCATION_PREFIX)
            break
        lines.append(line)
        length += len(line)
    return "".join(reversed(lines)).strip()
