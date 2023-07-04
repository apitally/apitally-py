from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List

import backoff
import httpx


logger = logging.getLogger(__name__)

BASE_URL = "https://ingest.apitally.io/v1/"


@dataclass(frozen=True)
class RequestKey:
    method: str
    path: str
    status_code: int


class Metrics:
    def __init__(self, client_id: str, send_every: float = 10) -> None:
        self.client_id = client_id
        self.send_every = send_every

        self.request_count: Counter[RequestKey] = Counter()
        self.response_times: Dict[RequestKey, List[float]] = {}

        self._lock = asyncio.Lock()
        self._stop_send_loop = False
        asyncio.create_task(self.run_send_loop())

    @property
    def base_url(self) -> str:
        return f"{BASE_URL}/{self.client_id}"

    async def log_request(self, method: str, path: str, status_code: int, response_time: float) -> None:
        key = RequestKey(method=method, path=path, status_code=status_code)
        async with self._lock:
            self.request_count[key] += 1
            self.response_times.setdefault(key, []).append(response_time)

    async def prepare_to_send(self) -> List[Dict[str, Any]]:
        data = []
        async with self._lock:
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

    async def send(self) -> None:
        if data := await self.prepare_to_send():
            logger.debug(f"Sending {data=}")
            await self._send(data)

    @backoff.on_exception(backoff.expo, httpx.HTTPError, max_time=10)
    async def _send(self, data: List[Dict[str, Any]]) -> None:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            response = await client.post(url="/", json=data)
            response.raise_for_status()

    async def run_send_loop(self) -> None:
        self._stop_send_loop = False
        while not self._stop_send_loop:
            try:
                await asyncio.sleep(self.send_every)
                await self.send()
            except Exception as e:
                logger.exception(e)

    def stop_send_loop(self) -> None:
        self._stop_send_loop = True
