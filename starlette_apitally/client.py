from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional
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
        self.start_send_loop()

    @property
    def base_url(self) -> str:
        return f"{INGESTER_BASE_URL}/{INGESTER_VERSION}/{self.client_id}/{self.env}"

    def start_send_loop(self) -> None:
        self._stop_send_loop = False
        asyncio.create_task(self._run_send_loop())

    async def _run_send_loop(self) -> None:
        while not self._stop_send_loop:
            try:
                await asyncio.sleep(self.send_every)
                await self.send_data()
            except Exception as e:
                logger.exception(e)

    def stop_send_loop(self) -> None:
        self._stop_send_loop = True

    def send_app_info(self, app: ASGIApp, app_version: Optional[str], openapi_url: Optional[str]) -> None:
        app_info = get_app_info(app, app_version, openapi_url)
        payload = {
            "instance_uuid": self.instance_uuid,
            "message_uuid": str(uuid4()),
        }
        payload.update(app_info)
        asyncio.create_task(self._send_request(url="/info", payload=payload))

    async def send_data(self) -> None:
        if requests := await self.metrics.get_and_reset_requests():
            load_averages = self.metrics.get_load_averages()
            payload: Dict[str, Any] = {
                "instance_uuid": self.instance_uuid,
                "message_uuid": str(uuid4()),
                "requests": requests,
            }
            if load_averages:
                payload["load_averages"] = load_averages
            await self._send_request(url="/data", payload=payload)

    @backoff.on_exception(backoff.expo, httpx.HTTPError, max_tries=3)
    async def _send_request(self, url: str, payload: Any) -> None:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            response = await client.post(url=url, json=payload)
            if response.status_code == 404:
                self.stop_send_loop()
                logger.error(f"Invalid Apitally client ID: {self.client_id}")
            elif response.status_code >= 500:
                response.raise_for_status()
            else:
                logger.error(
                    f"Got unexpected response from Apitally ingester {url} endpoint: [{response.status_code}] {response.text}"
                )
