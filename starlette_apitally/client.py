from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional
from uuid import uuid4

import backoff
import httpx
from starlette.types import ASGIApp

from starlette_apitally.app_info import get_app_info
from starlette_apitally.metrics import Metrics


logger = logging.getLogger(__name__)

INGESTER_BASE_URL = os.getenv("APITALLY_INGEST_BASE_URL") or "https://ingest.apitally.io"
INGESTER_VERSION = "v1"


class ApitallyClient:
    def __init__(self, client_id: str, env: str, send_every: float = 60) -> None:
        self.client_id = client_id
        self.env = env
        self.send_every = send_every
        self.instance_uuid = str(uuid4())
        self.metrics = Metrics()
        self._stop_send_loop = False
        asyncio.create_task(self.run_send_loop())

    @property
    def base_url(self) -> str:
        return f"{INGESTER_BASE_URL}/{INGESTER_VERSION}/{self.client_id}/{self.env}"

    async def run_send_loop(self) -> None:
        while not self._stop_send_loop:
            try:
                await asyncio.sleep(self.send_every)
                await self.send_data()
            except Exception as e:
                logger.exception(e)

    def stop_send_loop(self) -> None:
        self._stop_send_loop = True

    def send_app_info(self, app: ASGIApp, app_version: Optional[str], openapi_url: Optional[str]) -> None:
        app_info = get_app_info(app, openapi_url)
        if app_version:
            app_info["version"] = app_version
        asyncio.create_task(self._send_app_info(app_info))

    @backoff.on_exception(backoff.expo, httpx.HTTPError, max_tries=3, raise_on_giveup=False)
    async def _send_app_info(self, app_info: Dict[str, Any]) -> None:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            payload = {
                "message_uuid": str(uuid4()),
                "instance_uuid": self.instance_uuid,
            }
            payload.update(app_info)
            response = await client.post(url="/info", json=payload)
            if response.status_code == 404:
                self.stop_send_loop()
                logger.error(f"Invalid Apitally client ID: {self.client_id}")
            else:
                response.raise_for_status()

    async def send_data(self) -> None:
        if requests := await self.metrics.get_and_reset_requests():
            load_average = self.metrics.get_load_average()
            await self._send_data(requests, load_average)

    @backoff.on_exception(backoff.expo, httpx.HTTPError, max_tries=3)
    async def _send_data(self, requests: List[Dict[str, Any]], load_average: Optional[Dict[str, float]]) -> None:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
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
