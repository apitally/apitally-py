from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass(frozen=True)
class RequestKey:
    method: str
    path: str
    status_code: int


class RequestMetrics:
    def __init__(self) -> None:
        self.request_count: Counter[RequestKey] = Counter()
        self.response_times: Dict[RequestKey, List[float]] = {}
        self.lock = asyncio.Lock()

    async def log_request(self, method: str, path: str, status_code: int, response_time: float) -> None:
        key = RequestKey(method=method, path=path, status_code=status_code)
        async with self.lock:
            self.request_count[key] += 1
            self.response_times.setdefault(key, []).append(response_time)

    async def prepare_to_send(self) -> List[Dict[str, Any]]:
        data = []
        async with self.lock:
            for key, count in self.request_count.items():
                data.append(
                    {
                        "method": key.method,
                        "path": key.path,
                        "status_code": key.status_code,
                        "request_count": count,
                        "response_times": self.response_times.get(key) or [],
                    }
                )
            self.request_count.clear()
            self.response_times.clear()
        return data
