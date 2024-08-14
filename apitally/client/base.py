from __future__ import annotations

import asyncio
import contextlib
import os
import re
import threading
import time
import traceback
from abc import ABC
from collections import Counter
from dataclasses import dataclass
from math import floor
from typing import Any, Dict, List, Optional, Set, Tuple, Type, TypeVar, Union, cast
from uuid import UUID, uuid4

from apitally.client.logging import get_logger


logger = get_logger(__name__)

HUB_BASE_URL = os.getenv("APITALLY_HUB_BASE_URL") or "https://hub.apitally.io"
HUB_VERSION = "v2"
REQUEST_TIMEOUT = 10
MAX_QUEUE_TIME = 3600
SYNC_INTERVAL = 60
INITIAL_SYNC_INTERVAL = 10
INITIAL_SYNC_INTERVAL_DURATION = 3600
MAX_EXCEPTION_MSG_LENGTH = 2048
MAX_EXCEPTION_TRACEBACK_LENGTH = 65536

TApitallyClient = TypeVar("TApitallyClient", bound="ApitallyClientBase")


class ApitallyClientBase(ABC):
    _instance: Optional[ApitallyClientBase] = None
    _lock = threading.Lock()

    def __new__(cls: Type[TApitallyClient], *args, **kwargs) -> TApitallyClient:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cast(TApitallyClient, cls._instance)

    def __init__(self, client_id: str, env: str) -> None:
        if hasattr(self, "client_id"):
            raise RuntimeError("Apitally client is already initialized")  # pragma: no cover
        try:
            UUID(client_id)
        except ValueError:
            raise ValueError(f"invalid client_id '{client_id}' (expecting hexadecimal UUID format)")
        if re.match(r"^[\w-]{1,32}$", env) is None:
            raise ValueError(f"invalid env '{env}' (expecting 1-32 alphanumeric lowercase characters and hyphens only)")

        self.client_id = client_id
        self.env = env
        self.instance_uuid = str(uuid4())
        self.request_counter = RequestCounter()
        self.validation_error_counter = ValidationErrorCounter()
        self.server_error_counter = ServerErrorCounter()
        self.consumer_registry = ConsumerRegistry()

        self._startup_data: Optional[Dict[str, Any]] = None
        self._startup_data_sent = False
        self._started_at = time.time()

    @classmethod
    def get_instance(cls: Type[TApitallyClient]) -> TApitallyClient:
        if cls._instance is None:
            raise RuntimeError("Apitally client not initialized")  # pragma: no cover
        return cast(TApitallyClient, cls._instance)

    @property
    def sync_interval(self) -> float:
        return (
            SYNC_INTERVAL if time.time() - self._started_at > INITIAL_SYNC_INTERVAL_DURATION else INITIAL_SYNC_INTERVAL
        )

    @property
    def hub_url(self) -> str:
        return f"{HUB_BASE_URL}/{HUB_VERSION}/{self.client_id}/{self.env}"

    def add_uuids_to_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        data_with_uuids = {
            "instance_uuid": self.instance_uuid,
            "message_uuid": str(uuid4()),
        }
        data_with_uuids.update(data)
        return data_with_uuids

    def get_sync_data(self) -> Dict[str, Any]:
        data = {
            "requests": self.request_counter.get_and_reset_requests(),
            "validation_errors": self.validation_error_counter.get_and_reset_validation_errors(),
            "server_errors": self.server_error_counter.get_and_reset_server_errors(),
            "consumers": self.consumer_registry.get_and_reset_updated_consumers(),
        }
        return self.add_uuids_to_data(data)


@dataclass(frozen=True)
class RequestInfo:
    consumer: Optional[str]
    method: str
    path: str
    status_code: int


class RequestCounter:
    def __init__(self) -> None:
        self.request_counts: Counter[RequestInfo] = Counter()
        self.request_size_sums: Counter[RequestInfo] = Counter()
        self.response_size_sums: Counter[RequestInfo] = Counter()
        self.response_times: Dict[RequestInfo, Counter[int]] = {}
        self.request_sizes: Dict[RequestInfo, Counter[int]] = {}
        self.response_sizes: Dict[RequestInfo, Counter[int]] = {}
        self._lock = threading.Lock()

    def add_request(
        self,
        consumer: Optional[str],
        method: str,
        path: str,
        status_code: int,
        response_time: float,
        request_size: str | int | None = None,
        response_size: str | int | None = None,
    ) -> None:
        request_info = RequestInfo(
            consumer=consumer,
            method=method.upper(),
            path=path,
            status_code=status_code,
        )
        response_time_ms_bin = int(floor(response_time / 0.01) * 10)  # In ms, rounded down to nearest 10ms
        with self._lock:
            self.request_counts[request_info] += 1
            self.response_times.setdefault(request_info, Counter())[response_time_ms_bin] += 1
            if request_size is not None:
                with contextlib.suppress(ValueError):
                    request_size = int(request_size)
                    request_size_kb_bin = request_size // 1000  # In KB, rounded down to nearest 1KB
                    self.request_size_sums[request_info] += request_size
                    self.request_sizes.setdefault(request_info, Counter())[request_size_kb_bin] += 1
            if response_size is not None:
                with contextlib.suppress(ValueError):
                    response_size = int(response_size)
                    response_size_kb_bin = response_size // 1000  # In KB, rounded down to nearest 1KB
                    self.response_size_sums[request_info] += response_size
                    self.response_sizes.setdefault(request_info, Counter())[response_size_kb_bin] += 1

    def get_and_reset_requests(self) -> List[Dict[str, Any]]:
        data: List[Dict[str, Any]] = []
        with self._lock:
            for request_info, count in self.request_counts.items():
                data.append(
                    {
                        "consumer": request_info.consumer,
                        "method": request_info.method,
                        "path": request_info.path,
                        "status_code": request_info.status_code,
                        "request_count": count,
                        "request_size_sum": self.request_size_sums.get(request_info, 0),
                        "response_size_sum": self.response_size_sums.get(request_info, 0),
                        "response_times": self.response_times.get(request_info) or Counter(),
                        "request_sizes": self.request_sizes.get(request_info) or Counter(),
                        "response_sizes": self.response_sizes.get(request_info) or Counter(),
                    }
                )
            self.request_counts.clear()
            self.request_size_sums.clear()
            self.response_size_sums.clear()
            self.response_times.clear()
            self.request_sizes.clear()
            self.response_sizes.clear()
        return data


@dataclass(frozen=True)
class ValidationError:
    consumer: Optional[str]
    method: str
    path: str
    loc: Tuple[str, ...]
    msg: str
    type: str


class ValidationErrorCounter:
    def __init__(self) -> None:
        self.error_counts: Counter[ValidationError] = Counter()
        self._lock = threading.Lock()

    def add_validation_errors(
        self, consumer: Optional[str], method: str, path: str, detail: List[Dict[str, Any]]
    ) -> None:
        with self._lock:
            for error in detail:
                try:
                    validation_error = ValidationError(
                        consumer=consumer,
                        method=method.upper(),
                        path=path,
                        loc=tuple(str(loc) for loc in error["loc"]),
                        msg=error["msg"],
                        type=error["type"],
                    )
                    self.error_counts[validation_error] += 1
                except (KeyError, TypeError):  # pragma: no cover
                    pass

    def get_and_reset_validation_errors(self) -> List[Dict[str, Any]]:
        data: List[Dict[str, Any]] = []
        with self._lock:
            for validation_error, count in self.error_counts.items():
                data.append(
                    {
                        "consumer": validation_error.consumer,
                        "method": validation_error.method,
                        "path": validation_error.path,
                        "loc": validation_error.loc,
                        "msg": validation_error.msg,
                        "type": validation_error.type,
                        "error_count": count,
                    }
                )
            self.error_counts.clear()
        return data


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
        for line in traceback.format_exception(exception)[::-1]:
            if length + len(line) > cutoff:
                lines.append(prefix)
                break
            lines.append(line)
            length += len(line)
        return "".join(lines[::-1]).strip()


class Consumer:
    def __init__(self, identifier: str, name: Optional[str] = None, group: Optional[str] = None) -> None:
        self.identifier = str(identifier).strip()[:128]
        self.name = str(name).strip()[:64] if name else None
        self.group = str(group).strip()[:64] if group else None

    @classmethod
    def from_string_or_object(cls, consumer: Optional[Union[str, Consumer]]) -> Optional[Consumer]:
        if not consumer:
            return None
        if isinstance(consumer, Consumer):
            return consumer
        consumer = str(consumer).strip()
        if not consumer:
            return None
        return cls(identifier=consumer)

    def update(self, name: str | None = None, group: str | None = None) -> bool:
        name = str(name).strip()[:64] if name else None
        group = str(group).strip()[:64] if group else None
        updated = False
        if name and name != self.name:
            self.name = name
            updated = True
        if group and group != self.group:
            self.group = group
            updated = True
        return updated


class ConsumerRegistry:
    def __init__(self) -> None:
        self.consumers: Dict[str, Consumer] = {}
        self.updated: Set[str] = set()
        self._lock = threading.Lock()

    def add_or_update_consumer(self, consumer: Optional[Consumer]) -> None:
        if not consumer or (not consumer.name and not consumer.group):
            return  # Only register consumers with name or group set
        with self._lock:
            if consumer.identifier not in self.consumers:
                self.consumers[consumer.identifier] = consumer
                self.updated.add(consumer.identifier)
            elif self.consumers[consumer.identifier].update(name=consumer.name, group=consumer.group):
                self.updated.add(consumer.identifier)

    def get_and_reset_updated_consumers(self) -> List[Dict[str, Any]]:
        data: List[Dict[str, Any]] = []
        with self._lock:
            for identifier in self.updated:
                if consumer := self.consumers.get(identifier):
                    data.append(
                        {
                            "identifier": consumer.identifier,
                            "name": str(consumer.name)[:64] if consumer.name else None,
                            "group": str(consumer.group)[:64] if consumer.group else None,
                        }
                    )
            self.updated.clear()
        return data
