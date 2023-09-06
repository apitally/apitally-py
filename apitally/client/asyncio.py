from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Dict, Optional

import backoff
import httpx

from apitally.client.base import ApitallyClientBase, handle_retry_giveup


logger = logging.getLogger(__name__)
retry = backoff.on_exception(
    backoff.expo,
    httpx.HTTPError,
    max_tries=3,
    on_giveup=handle_retry_giveup,
    raise_on_giveup=False,
)


class ApitallyClient(ApitallyClientBase):
    def __init__(self, client_id: str, env: str, sync_api_keys: bool = False, sync_interval: float = 60) -> None:
        super().__init__(client_id=client_id, env=env, sync_api_keys=sync_api_keys, sync_interval=sync_interval)
        self._stop_sync_loop = False
        self._sync_loop_task: Optional[asyncio.Task[Any]] = None

    def get_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.hub_url, timeout=1)

    def start_sync_loop(self) -> None:
        self._stop_sync_loop = False
        self._sync_loop_task = asyncio.create_task(self._run_sync_loop())

    async def _run_sync_loop(self) -> None:
        if self.sync_api_keys:
            try:
                async with self.get_http_client() as client:
                    await self.get_keys(client)
            except Exception as e:
                logger.exception(e)
        while not self._stop_sync_loop:
            try:
                await asyncio.sleep(self.sync_interval)
                async with self.get_http_client() as client:
                    await self.send_requests_data(client)
                    if self.sync_api_keys:
                        await self.get_keys(client)
            except Exception as e:  # pragma: no cover
                logger.exception(e)

    def stop_sync_loop(self) -> None:
        self._stop_sync_loop = True

    def send_app_info(self, app_info: Dict[str, Any]) -> None:
        payload = self.get_info_payload(app_info)
        asyncio.create_task(self._send_app_info(payload=payload))

    async def send_requests_data(self, client: httpx.AsyncClient) -> None:
        payload = self.get_requests_payload()
        await self._send_requests_data(client, payload)

    async def get_keys(self, client: httpx.AsyncClient) -> None:
        if response_data := await self._get_keys(client):  # Response data can be None if backoff gives up
            self.handle_keys_response(response_data)
        elif self.key_registry.salt is None:
            logger.error("Initial Apitally API key sync failed")
            # Exit because the application will not be able to authenticate requests
            sys.exit(1)

    @retry
    async def _send_app_info(self, payload: Dict[str, Any]) -> None:
        async with self.get_http_client() as client:
            response = await client.post(url="/info", json=payload, timeout=1)
            if response.status_code == 404 and "Client ID" in response.text:
                self.stop_sync_loop()
                logger.error(f"Invalid Apitally client ID {self.client_id}")
            else:
                response.raise_for_status()

    @retry
    async def _send_requests_data(self, client: httpx.AsyncClient, payload: Dict[str, Any]) -> None:
        response = await client.post(url="/requests", json=payload)
        response.raise_for_status()

    @retry
    async def _get_keys(self, client: httpx.AsyncClient) -> Dict[str, Any]:
        response = await client.get(url="/keys")
        response.raise_for_status()
        return response.json()
