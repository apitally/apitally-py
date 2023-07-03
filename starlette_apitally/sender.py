from __future__ import annotations

import asyncio
import logging

import backoff
import httpx

from starlette_apitally.metrics import RequestMetrics


logger = logging.getLogger(__name__)

BASE_URL = "https://ingest.apitally.io/v1/"


class Sender:
    def __init__(self, metrics: RequestMetrics, client_id: str, send_every: int = 10) -> None:
        self.metrics = metrics
        self.client_id = client_id
        self.send_every = send_every
        asyncio.create_task(self.send_loop())

    @backoff.on_exception(backoff.expo, httpx.HTTPError, max_time=10)
    async def send(self) -> None:
        if data := await self.metrics.prepare_to_send():
            logger.debug(f"Sending {data=}")
            async with httpx.AsyncClient(base_url=f"{BASE_URL}/{self.client_id}") as client:
                response = await client.post(url="/", json=data)
                response.raise_for_status()

    async def send_loop(self) -> None:
        await asyncio.sleep(self.send_every)
        try:
            await self.send()
        except Exception as e:
            logger.exception(e)
        finally:
            asyncio.create_task(self.send_loop())
