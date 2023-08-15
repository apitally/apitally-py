from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from math import floor
from typing import Any, Dict, List


@dataclass(frozen=True)
class RequestInfo:
    method: str
    path: str
    status_code: int


class RequestLogger:
    def __init__(self) -> None:
        self.request_count: Counter[RequestInfo] = Counter()
        self.response_times: Dict[RequestInfo, Counter[int]] = {}
        self.lock = asyncio.Lock()

    def log_request(self, method: str, path: str, status_code: int, response_time: float) -> None:
        request_info = RequestInfo(method=method, path=path, status_code=status_code)
        response_time_ms_bin = int(floor(response_time / 0.01) * 10)  # In ms, rounded down to nearest 10ms
        self.request_count[request_info] += 1
        self.response_times.setdefault(request_info, Counter())[response_time_ms_bin] += 1

    def get_and_reset_requests(self) -> List[Dict[str, Any]]:
        data: List[Dict[str, Any]] = []
        for request_info, count in self.request_count.items():
            data.append(
                {
                    "method": request_info.method,
                    "path": request_info.path,
                    "status_code": request_info.status_code,
                    "request_count": count,
                    "response_times": self.response_times.get(request_info) or Counter(),
                }
            )
        self.request_count.clear()
        self.response_times.clear()
        return data
