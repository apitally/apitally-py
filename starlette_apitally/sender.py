from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import backoff
import httpx

from starlette_apitally.metrics import RequestMetrics


logger = logging.getLogger(__name__)

BASE_URL = "https://ingest.apitally.io/v1/"


class Sender:
    def __init__(self, metrics: RequestMetrics, client_id: str, send_every: float = 10) -> None:
        self.metrics = metrics
        self.client_id = client_id
        self.send_every = send_every
        self._stop_loop = False
        asyncio.create_task(self.run_loop())

    @property
    def base_url(self) -> str:
        return f"{BASE_URL}/{self.client_id}"

    async def send(self) -> None:
        if data := await self.metrics.prepare_to_send():
            logger.debug(f"Sending {data=}")
            await self._send(data)

    @backoff.on_exception(backoff.expo, httpx.HTTPError, max_time=10)
    async def _send(self, data: List[Dict[str, Any]]) -> None:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            response = await client.post(url="/", json=data)
            response.raise_for_status()

    async def run_loop(self) -> None:
        self._stop_loop = False
        while not self._stop_loop:
            try:
                await asyncio.sleep(self.send_every)
                await self.send()
            except Exception as e:
                logger.exception(e)

    def stop_loop(self) -> None:
        self._stop_loop = True
