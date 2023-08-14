from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from math import floor
from typing import Any, Dict, List


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    status_code: int


class Requests:
    def __init__(self) -> None:
        self.request_count: Counter[Request] = Counter()
        self.response_times: Dict[Request, Counter[int]] = {}
        self.lock = asyncio.Lock()

    def log_request(self, method: str, path: str, status_code: int, response_time: float) -> None:
        key = Request(method=method, path=path, status_code=status_code)
        response_time_ms_bin = int(floor(response_time / 0.01) * 10)  # In ms, rounded down to nearest 10ms
        self.request_count[key] += 1
        self.response_times.setdefault(key, Counter())[response_time_ms_bin] += 1

    def get_and_reset_requests(self) -> List[Dict[str, Any]]:
        data: List[Dict[str, Any]] = []
        for key, count in self.request_count.items():
            data.append(
                {
                    "method": key.method,
                    "path": key.path,
                    "status_code": key.status_code,
                    "request_count": count,
                    "response_times": self.response_times.get(key) or Counter(),
                }
            )
        self.request_count.clear()
        self.response_times.clear()
        return data
