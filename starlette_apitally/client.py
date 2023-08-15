from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, Dict, Optional
from uuid import uuid4

import backoff
import httpx

from starlette_apitally.keys import Keys
from starlette_apitally.requests import Requests


logger = logging.getLogger(__name__)

HUB_BASE_URL = os.getenv("APITALLY_HUB_BASE_URL") or "https://hub.apitally.io"
HUB_VERSION = "v1"


def handle_retry_giveup(details) -> None:
    logger.error("Apitally client failed to sync with hub: {target.__name__}: {exception}".format(**details))


retry = backoff.on_exception(
    backoff.expo, httpx.HTTPError, max_tries=3, on_giveup=handle_retry_giveup, raise_on_giveup=False
)


class ApitallyClient:
    _instance: Optional[ApitallyClient] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs) -> ApitallyClient:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, client_id: str, env: str, enable_keys: bool = False, send_every: float = 60) -> None:
        self.enable_keys = enable_keys
        self.send_every = send_every

        if hasattr(self, "client_id") and hasattr(self, "env"):
            if getattr(self, "client_id") != client_id or getattr(self, "env") != env:
                raise RuntimeError("Apitally client is already initialized with different client_id or env")
            return

        self.client_id = client_id
        self.env = env
        self.instance_uuid = str(uuid4())
        self.requests = Requests()
        self.keys = Keys()
        self._stop_sync_loop = False
        self.start_sync_loop()

    @classmethod
    def get_instance(cls) -> ApitallyClient:
        if cls._instance is None:
            raise RuntimeError("Apitally client not initialized")
        return cls._instance

    def get_http_client(self) -> httpx.AsyncClient:
        base_url = f"{HUB_BASE_URL}/{HUB_VERSION}/{self.client_id}/{self.env}"
        return httpx.AsyncClient(base_url=base_url)

    def start_sync_loop(self) -> None:
        self._stop_sync_loop = False
        if self.enable_keys:
            asyncio.create_task(self.get_keys())
        asyncio.create_task(self._run_sync_loop())

    async def _run_sync_loop(self) -> None:
        while not self._stop_sync_loop:
            try:
                await asyncio.sleep(self.send_every)
                async with self.get_http_client() as client:
                    await self.send_requests_data(client)
                    if self.enable_keys:
                        await self._get_keys(client)
            except Exception as e:
                logger.exception(e)

    def stop_sync_loop(self) -> None:
        self._stop_sync_loop = True

    def send_app_info(self, app_info: Dict[str, Any]) -> None:
        payload = {
            "instance_uuid": self.instance_uuid,
            "message_uuid": str(uuid4()),
        }
        payload.update(app_info)
        asyncio.create_task(self._send_app_info(payload=payload))

    @retry
    async def _send_app_info(self, payload: Any) -> None:
        async with self.get_http_client() as client:
            response = await client.post(url="/info", json=payload)
            if response.status_code == 404 and "Client ID" in response.text:
                self.stop_sync_loop()
                logger.error(f"Invalid Apitally client ID {self.client_id}")
            elif response.status_code >= 400:
                response.raise_for_status()

    async def send_requests_data(self, client: httpx.AsyncClient) -> None:
        requests = self.requests.get_and_reset_requests()
        used_key_ids = self.keys.get_and_reset_used_key_ids() if self.enable_keys else []
        payload: Dict[str, Any] = {
            "instance_uuid": self.instance_uuid,
            "message_uuid": str(uuid4()),
            "requests": requests,
            "used_key_ids": used_key_ids,
        }
        await self._send_requests_data(client, payload)

    @retry
    async def _send_requests_data(self, client: httpx.AsyncClient, payload: Any) -> None:
        response = await client.post(url="/requests", json=payload)
        response.raise_for_status()

    async def get_keys(self) -> None:
        async with self.get_http_client() as client:
            await self._get_keys(client)

    @retry
    async def _get_keys(self, client: httpx.AsyncClient) -> None:
        response = await client.get(url="/keys")
        response.raise_for_status()
        response_data = response.json()
        self.keys.salt = response_data["salt"]
        self.keys.update(response_data["keys"])
