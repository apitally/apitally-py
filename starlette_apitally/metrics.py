from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from math import floor
from typing import Any, Dict, List


@dataclass(frozen=True)
class RequestKey:
    method: str
    path: str
    status_code: int


class Metrics:
    def __init__(self) -> None:
        self.request_count: Counter[RequestKey] = Counter()
        self.response_times: Dict[RequestKey, Counter[int]] = {}
        self.lock = asyncio.Lock()

    async def log_request(self, method: str, path: str, status_code: int, response_time: float) -> None:
        key = RequestKey(method=method, path=path, status_code=status_code)
        response_time_ms_bin = int(floor(response_time / 0.01) * 10)  # In ms, rounded down to nearest 10ms
        async with self.lock:
            self.request_count[key] += 1
            self.response_times.setdefault(key, Counter())[response_time_ms_bin] += 1

    async def get_and_reset_requests(self) -> List[Dict[str, Any]]:
        data: List[Dict[str, Any]] = []
        async with self.lock:
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
