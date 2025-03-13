from __future__ import annotations

import asyncio
import sys
import threading
import traceback
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from apitally.client.sentry import get_sentry_event_id_async


MAX_EXCEPTION_MSG_LENGTH = 2048
MAX_EXCEPTION_TRACEBACK_LENGTH = 65536


@dataclass(frozen=True)
class ServerError:
    consumer: Optional[str]
    method: str
    path: str
    type: str
    msg: str
    traceback: str


class ServerErrorCounter:
    def __init__(self) -> None:
        self.error_counts: Counter[ServerError] = Counter()
        self.sentry_event_ids: Dict[ServerError, str] = {}
        self._lock = threading.Lock()
        self._tasks: Set[asyncio.Task] = set()

    def add_server_error(self, consumer: Optional[str], method: str, path: str, exception: BaseException) -> None:
        if not isinstance(exception, BaseException):
            return  # pragma: no cover
        with self._lock:
            server_error = ServerError(
                consumer=consumer,
                method=method.upper(),
                path=path,
                type=get_exception_type(exception),
                msg=get_truncated_exception_msg(exception),
                traceback=get_truncated_exception_traceback(exception),
            )
            self.error_counts[server_error] += 1
        get_sentry_event_id_async(lambda event_id: self.sentry_event_ids.update({server_error: event_id}))

    def get_and_reset_server_errors(self) -> List[Dict[str, Any]]:
        data: List[Dict[str, Any]] = []
        with self._lock:
            for server_error, count in self.error_counts.items():
                data.append(
                    {
                        "consumer": server_error.consumer,
                        "method": server_error.method,
                        "path": server_error.path,
                        "type": server_error.type,
                        "msg": server_error.msg,
                        "traceback": server_error.traceback,
                        "sentry_event_id": self.sentry_event_ids.get(server_error),
                        "error_count": count,
                    }
                )
            self.error_counts.clear()
            self.sentry_event_ids.clear()
        return data


def get_exception_type(exception: BaseException) -> str:
    exception_type = type(exception)
    return f"{exception_type.__module__}.{exception_type.__qualname__}"


def get_truncated_exception_msg(exception: BaseException) -> str:
    msg = str(exception).strip()
    if len(msg) <= MAX_EXCEPTION_MSG_LENGTH:
        return msg
    suffix = "... (truncated)"
    cutoff = MAX_EXCEPTION_MSG_LENGTH - len(suffix)
    return msg[:cutoff] + suffix


def get_truncated_exception_traceback(exception: BaseException) -> str:
    prefix = "... (truncated) ...\n"
    cutoff = MAX_EXCEPTION_TRACEBACK_LENGTH - len(prefix)
    lines = []
    length = 0
    if sys.version_info >= (3, 10):
        traceback_lines = traceback.format_exception(exception)
    else:
        traceback_lines = traceback.format_exception(type(exception), exception, exception.__traceback__)
    for line in traceback_lines[::-1]:
        if length + len(line) > cutoff:
            lines.append(prefix)
            break
        lines.append(line)
        length += len(line)
    return "".join(lines[::-1]).strip()
