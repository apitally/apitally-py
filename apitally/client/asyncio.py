from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from typing import Any, Dict, Optional, Tuple

import backoff
import httpx

from apitally.client.base import MAX_QUEUE_TIME, REQUEST_TIMEOUT, ApitallyClientBase
from apitally.client.logging import get_logger


logger = get_logger(__name__)
retry = partial(
    backoff.on_exception,
    backoff.expo,
    httpx.HTTPError,
    max_tries=3,
    logger=logger,
    giveup_log_level=logging.WARNING,
)


class ApitallyClient(ApitallyClientBase):
    def __init__(self, client_id: str, env: str) -> None:
        super().__init__(client_id=client_id, env=env)
        self._stop_sync_loop = False
        self._sync_loop_task: Optional[asyncio.Task[Any]] = None
        self._sync_data_queue: asyncio.Queue[Tuple[float, Dict[str, Any]]] = asyncio.Queue()

    def get_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.hub_url, timeout=REQUEST_TIMEOUT)

    def start_sync_loop(self) -> None:
        self._stop_sync_loop = False
        self._sync_loop_task = asyncio.create_task(self._run_sync_loop())

    async def _run_sync_loop(self) -> None:
        first_iteration = True
        while not self._stop_sync_loop:
            try:
                time_start = time.perf_counter()
                async with self.get_http_client() as client:
                    tasks = [self.send_sync_data(client)]
                    if not self._startup_data_sent and not first_iteration:
                        tasks.append(self.send_startup_data(client))
                    await asyncio.gather(*tasks)
                time_elapsed = time.perf_counter() - time_start
                await asyncio.sleep(self.sync_interval - time_elapsed)
            except Exception as e:  # pragma: no cover
                logger.exception(e)
            first_iteration = False

    def stop_sync_loop(self) -> None:
        self._stop_sync_loop = True

    async def handle_shutdown(self) -> None:
        if self._sync_loop_task is not None:
            self._sync_loop_task.cancel()
        # Send any remaining data before exiting
        async with self.get_http_client() as client:
            await self.send_sync_data(client)

    def set_startup_data(self, data: Dict[str, Any]) -> None:
        self._startup_data_sent = False
        self._startup_data = self.add_uuids_to_data(data)
        asyncio.create_task(self._set_startup_data_task())

    async def _set_startup_data_task(self) -> None:
        async with self.get_http_client() as client:
            await self.send_startup_data(client)

    async def send_startup_data(self, client: httpx.AsyncClient) -> None:
        if self._startup_data is not None:
            await self._send_startup_data(client, self._startup_data)

    async def send_sync_data(self, client: httpx.AsyncClient) -> None:
        data = self.get_sync_data()
        self._sync_data_queue.put_nowait((time.time(), data))

        failed_items = []
        while not self._sync_data_queue.empty():
            timestamp, data = self._sync_data_queue.get_nowait()
            try:
                if (time_offset := time.time() - timestamp) <= MAX_QUEUE_TIME:
                    data["time_offset"] = time_offset
                    await self._send_sync_data(client, data)
                self._sync_data_queue.task_done()
            except httpx.HTTPError:
                failed_items.append((timestamp, data))
        for item in failed_items:
            self._sync_data_queue.put_nowait(item)

    @retry(raise_on_giveup=False)
    async def _send_startup_data(self, client: httpx.AsyncClient, data: Dict[str, Any]) -> None:
        logger.debug("Sending startup data")
        response = await client.post(url="/startup", json=data, timeout=REQUEST_TIMEOUT)
        self._handle_hub_response(response)
        self._startup_data_sent = True
        self._startup_data = None

    @retry()
    async def _send_sync_data(self, client: httpx.AsyncClient, data: Dict[str, Any]) -> None:
        logger.debug("Synchronizing data with hub")
        response = await client.post(url="/sync", json=data)
        self._handle_hub_response(response)

    def _handle_hub_response(self, response: httpx.Response) -> None:
        if response.status_code == 404:
            self.stop_sync_loop()
            logger.error("Invalid Apitally client ID: %s", self.client_id)
        elif response.status_code == 422:
            logger.error("Received validation error from hub: %s", response.json())
        else:
            response.raise_for_status()
