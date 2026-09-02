from __future__ import annotations

import sys
import threading
import traceback
from contextvars import ContextVar
from dataclasses import dataclass
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
MAX_GROUPS = 100
MAX_METHOD_LENGTH = 12
MAX_PATH_LENGTH = 2_000
MAX_EXCEPTION_TYPE_LENGTH = 256
MAX_EXCEPTION_MESSAGE_LENGTH = 2_048
MAX_STACKTRACE_LENGTH = 65_536
MAX_SENTRY_EVENT_ID_LENGTH = 32
MAX_COUNT = 2**32 - 1
MESSAGE_TRUNCATION_SUFFIX = "... (truncated)"
STACKTRACE_TRUNCATION_PREFIX = "... (truncated) ...\n"


@dataclass(slots=True, eq=False)
class ExceptionHolder:
    exception: BaseException | None = None
    sentry_event_id: str | None = None
    # Allows outer Sentry middleware to enrich an aggregate after request finalization.
    server_error_key: ServerErrorKey | None = None


@dataclass(frozen=True, slots=True)
class ServerErrorKey:
    consumer: str | None
    method: str
    path: str
    type: str
    message: str
    stacktrace: str


exception_holder_var: ContextVar[ExceptionHolder | None] = ContextVar("apitally_exception_holder", default=None)
server_error_lock = threading.Lock()
server_error_aggregates: dict[ServerErrorKey, tuple[int, str | None]] = {}


def init_exception_holder() -> ExceptionHolder:
    holder = ExceptionHolder()
    exception_holder_var.set(holder)
    return holder


def reset_exception_holder() -> None:
    exception_holder_var.set(None)


def set_exception(exception: BaseException, holder: ExceptionHolder | None = None) -> None:
    holder = holder or exception_holder_var.get()
    if holder is None:
        return
    exception = collapse_exception_group(exception)
    if holder.exception is not None and holder.exception is not exception:
        holder.sentry_event_id = None
        holder.server_error_key = None
    holder.exception = exception


def set_sentry_event_id(event_id: str) -> None:
    holder = exception_holder_var.get()
    if holder is None:
        return
    event_id = event_id.strip()[:MAX_SENTRY_EVENT_ID_LENGTH]
    if not event_id:
        return
    holder.sentry_event_id = event_id
    if holder.server_error_key is not None:
        with server_error_lock:
            aggregate = server_error_aggregates.get(holder.server_error_key)
            if aggregate is not None:
                server_error_aggregates[holder.server_error_key] = (aggregate[0], event_id)


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
        consumer=consumer,
        method=method[:MAX_METHOD_LENGTH],
        path=path[:MAX_PATH_LENGTH],
        type=format_exception_type(exception),
        message=format_exception_message(exception),
        stacktrace=format_exception_stacktrace(exception),
    )
    with server_error_lock:
        aggregate = server_error_aggregates.get(key)
        if aggregate is not None:
            count, sentry_event_id = aggregate
            server_error_aggregates[key] = (
                count + 1,
                exception_holder.sentry_event_id or sentry_event_id,
            )
        elif len(server_error_aggregates) < MAX_GROUPS:
            server_error_aggregates[key] = (1, exception_holder.sentry_event_id)
        else:
            return
        exception_holder.server_error_key = key


def drain_server_errors() -> list[dict[str, Any]]:
    global server_error_aggregates
    with server_error_lock:
        aggregates = server_error_aggregates
        server_error_aggregates = {}
    events = []
    for error, (count, sentry_event_id) in aggregates.items():
        body: dict[str, Any] = {
            "method": error.method,
            "path": error.path,
            "type": error.type,
            "message": error.message,
            "stacktrace": error.stacktrace,
            "count": min(count, MAX_COUNT),
        }
        if error.consumer is not None:
            body["consumer"] = error.consumer
        if sentry_event_id is not None:
            body["sentry_event_id"] = sentry_event_id
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
