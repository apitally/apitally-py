from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import traceback
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set


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
        exception_type = type(exception)
        with self._lock:
            server_error = ServerError(
                consumer=consumer,
                method=method.upper(),
                path=path,
                type=f"{exception_type.__module__}.{exception_type.__qualname__}",
                msg=self._get_truncated_exception_msg(exception),
                traceback=self._get_truncated_exception_traceback(exception),
            )
            self.error_counts[server_error] += 1
            self.capture_sentry_event_id(server_error)

    def capture_sentry_event_id(self, server_error: ServerError) -> None:
        try:
            from sentry_sdk.hub import Hub
            from sentry_sdk.scope import Scope
        except ImportError:
            return  # pragma: no cover
        if not hasattr(Scope, "get_isolation_scope") or not hasattr(Scope, "_last_event_id"):
            # sentry-sdk < 2.2.0 is not supported
            return  # pragma: no cover
        if Hub.current.client is None:
            return  # sentry-sdk not initialized

        scope = Scope.get_isolation_scope()
        if event_id := scope._last_event_id:
            self.sentry_event_ids[server_error] = event_id
            return

        async def _wait_for_sentry_event_id(scope: Scope) -> None:
            i = 0
            while not (event_id := scope._last_event_id) and i < 100:
                i += 1
                await asyncio.sleep(0.001)
            if event_id:
                self.sentry_event_ids[server_error] = event_id

        with contextlib.suppress(RuntimeError):  # ignore no running loop
            loop = asyncio.get_running_loop()
            task = loop.create_task(_wait_for_sentry_event_id(scope))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

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

    @staticmethod
    def _get_truncated_exception_msg(exception: BaseException) -> str:
        msg = str(exception).strip()
        if len(msg) <= MAX_EXCEPTION_MSG_LENGTH:
            return msg
        suffix = "... (truncated)"
        cutoff = MAX_EXCEPTION_MSG_LENGTH - len(suffix)
        return msg[:cutoff] + suffix

    @staticmethod
    def _get_truncated_exception_traceback(exception: BaseException) -> str:
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
