from __future__ import annotations

import asyncio
import logging
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional, TypedDict

import backoff
import httpx
import starlette

import starlette_apitally


logger = logging.getLogger(__name__)

INGEST_BASE_URL = "https://ingest.apitally.io/v1/"


@dataclass(frozen=True)
class RequestKey:
    method: str
    path: str
    status_code: int


class IngestDataItem(TypedDict):
    method: str
    path: str
    status_code: int
    request_count: int
    response_times: List[float]


class VersionsData(TypedDict):
    app_version: Optional[str]
    client_version: str
    starlette_version: str
    python_version: str


class Metrics:
    def __init__(self, client_id: str, app_version: Optional[str] = None, send_every: float = 10) -> None:
        self.client_id = client_id
        self.app_version = app_version
        self.send_every = send_every

        self.request_count: Counter[RequestKey] = Counter()
        self.response_times: Dict[RequestKey, List[float]] = {}

        self._lock = asyncio.Lock()
        self._stop_send_loop = False

        asyncio.create_task(self.run_send_loop())

    @property
    def ingest_base_url(self) -> str:
        return f"{INGEST_BASE_URL}/{self.client_id}"

    async def log_request(self, method: str, path: str, status_code: int, response_time: float) -> None:
        key = RequestKey(method=method, path=path, status_code=status_code)
        async with self._lock:
            self.request_count[key] += 1
            self.response_times.setdefault(key, []).append(response_time)

    async def prepare_to_send(self) -> List[IngestDataItem]:
        data: List[IngestDataItem] = []
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

    @backoff.on_exception(backoff.expo, httpx.HTTPError, max_tries=3)
    async def _send(self, data: List[IngestDataItem]) -> None:
        async with httpx.AsyncClient(base_url=self.ingest_base_url) as client:
            response = await client.post(url="/", json=data)
            response.raise_for_status()

    def send_versions(self) -> None:
        versions: VersionsData = {
            "app_version": self.app_version,
            "client_version": starlette_apitally.__version__,
            "starlette_version": starlette.__version__,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        }
        asyncio.create_task(self._send_versions(versions))

    @backoff.on_exception(backoff.expo, httpx.HTTPError, max_tries=3, raise_on_giveup=False)
    async def _send_versions(self, versions: VersionsData) -> None:
        async with httpx.AsyncClient(base_url=self.ingest_base_url) as client:
            response = await client.post(url="/versions", json=versions)
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
