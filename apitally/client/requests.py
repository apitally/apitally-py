from __future__ import annotations

import contextlib
import threading
from collections import Counter
from dataclasses import dataclass
from math import floor
from typing import Any, Dict, List, Optional


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
