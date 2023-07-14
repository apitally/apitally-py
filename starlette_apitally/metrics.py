from __future__ import annotations

import asyncio
import logging
import os
from collections import Counter
from dataclasses import dataclass
from math import floor
from typing import Any, Dict, List, Optional, TypedDict
from uuid import uuid4

import backoff
import httpx


logger = logging.getLogger(__name__)

INGEST_BASE_URL = os.getenv("APITALLY_INGEST_BASE_URL") or "https://ingest.apitally.io"


@dataclass(frozen=True)
class RequestKey:
    method: str
    path: str
    status_code: int


class RequestsDataItem(TypedDict):
    method: str
    path: str
    status_code: int
    request_count: int
    response_times: Counter[int]


class Metrics:
    def __init__(self, client_id: str, env: str, send_every: float = 60) -> None:
        self.client_id = client_id
        self.env = env
        self.send_every = send_every

        self.instance_uuid = str(uuid4())
        self.request_count: Counter[RequestKey] = Counter()
        self.response_times: Dict[RequestKey, Counter[int]] = {}

        self._lock = asyncio.Lock()
        self._stop_send_loop = False

        asyncio.create_task(self.run_send_loop())

    @property
    def ingest_base_url(self) -> str:
        return f"{INGEST_BASE_URL}/v1/{self.client_id}/{self.env}"

    async def log_request(self, method: str, path: str, status_code: int, response_time: float) -> None:
        key = RequestKey(method=method, path=path, status_code=status_code)
        response_time_ms_bin = int(floor(response_time / 0.01) * 10)  # In ms, rounded down to nearest 10ms
        async with self._lock:
            self.request_count[key] += 1
            self.response_times.setdefault(key, Counter())[response_time_ms_bin] += 1

    async def get_and_reset_requests(self) -> List[RequestsDataItem]:
        data: List[RequestsDataItem] = []
        async with self._lock:
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

    def get_load_average(self) -> Optional[Dict[str, float]]:
        try:
            avg_load = os.getloadavg()
            return {"1m": avg_load[0], "5m": avg_load[1], "15m": avg_load[2]}
        except (OSError, AttributeError):
            return None

    async def send_data(self) -> None:
        if requests := await self.get_and_reset_requests():
            load_average = self.get_load_average()
            await self._send_data(requests, load_average)

    @backoff.on_exception(backoff.expo, httpx.HTTPError, max_tries=3)
    async def _send_data(self, requests: List[RequestsDataItem], load_average: Optional[Dict[str, float]]) -> None:
        async with httpx.AsyncClient(base_url=self.ingest_base_url) as client:
            payload = {
                "message_uuid": str(uuid4()),
                "instance_uuid": self.instance_uuid,
                "requests": requests,
                "load_average": load_average,
            }
            response = await client.post(url="/data", json=payload)
            if response.status_code == 404:
                self.stop_send_loop()
                logger.error(f"Invalid Apitally client ID: {self.client_id}")
            else:
                response.raise_for_status()

    def send_app_info(self, versions: Dict[str, str | None], openapi: Optional[Dict[str, Any]]) -> None:
        asyncio.create_task(self._send_app_info(versions, openapi))

    @backoff.on_exception(backoff.expo, httpx.HTTPError, max_tries=3, raise_on_giveup=False)
    async def _send_app_info(self, versions: Dict[str, str | None], openapi: Optional[Dict[str, Any]]) -> None:
        async with httpx.AsyncClient(base_url=self.ingest_base_url) as client:
            payload = {
                "message_uuid": str(uuid4()),
                "instance_uuid": self.instance_uuid,
                "versions": versions,
                "openapi": openapi,
            }
            response = await client.post(url="/info", json=payload)
            if response.status_code == 404:
                self.stop_send_loop()
                logger.error(f"Invalid Apitally client ID: {self.client_id}")
            else:
                response.raise_for_status()

    async def run_send_loop(self) -> None:
        while not self._stop_send_loop:
            try:
                await asyncio.sleep(self.send_every)
                await self.send_data()
            except Exception as e:
                logger.exception(e)

    def stop_send_loop(self) -> None:
        self._stop_send_loop = True
