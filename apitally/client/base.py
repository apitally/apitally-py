from __future__ import annotations

import contextlib
import os
import re
import threading
import time
from abc import ABC
from collections import Counter
from dataclasses import dataclass
from math import floor
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar, cast
from uuid import UUID, uuid4

from apitally.client.logging import get_logger


logger = get_logger(__name__)

HUB_BASE_URL = os.getenv("APITALLY_HUB_BASE_URL") or "https://hub.apitally.io"
HUB_VERSION = "v1"
REQUEST_TIMEOUT = 10
MAX_QUEUE_TIME = 3600
SYNC_INTERVAL = 60
INITIAL_SYNC_INTERVAL = 10
INITIAL_SYNC_INTERVAL_DURATION = 3600

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

        self._app_info_payload: Optional[Dict[str, Any]] = None
        self._app_info_sent = False
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

    def get_info_payload(self, app_info: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "instance_uuid": self.instance_uuid,
            "message_uuid": str(uuid4()),
        }
        payload.update(app_info)
        return payload

    def get_requests_payload(self) -> Dict[str, Any]:
        requests = self.request_counter.get_and_reset_requests()
        validation_errors = self.validation_error_counter.get_and_reset_validation_errors()
        return {
            "instance_uuid": self.instance_uuid,
            "message_uuid": str(uuid4()),
            "requests": requests,
            "validation_errors": validation_errors,
        }


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
