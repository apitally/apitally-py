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
        self._requests_data_queue: asyncio.Queue[Tuple[float, Dict[str, Any]]] = asyncio.Queue()

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
                    tasks = [self.send_requests_data(client)]
                    if not self._app_info_sent and not first_iteration:
                        tasks.append(self.send_app_info(client))
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
        # Send any remaining requests data before exiting
        async with self.get_http_client() as client:
            await self.send_requests_data(client)

    def set_app_info(self, app_info: Dict[str, Any]) -> None:
        self._app_info_sent = False
        self._app_info_payload = self.get_info_payload(app_info)
        asyncio.create_task(self._set_app_info_task())

    async def _set_app_info_task(self) -> None:
        async with self.get_http_client() as client:
            await self.send_app_info(client)

    async def send_app_info(self, client: httpx.AsyncClient) -> None:
        if self._app_info_payload is not None:
            await self._send_app_info(client, self._app_info_payload)

    async def send_requests_data(self, client: httpx.AsyncClient) -> None:
        payload = self.get_requests_payload()
        self._requests_data_queue.put_nowait((time.time(), payload))

        failed_items = []
        while not self._requests_data_queue.empty():
            payload_time, payload = self._requests_data_queue.get_nowait()
            try:
                if (time_offset := time.time() - payload_time) <= MAX_QUEUE_TIME:
                    payload["time_offset"] = time_offset
                    await self._send_requests_data(client, payload)
                self._requests_data_queue.task_done()
            except httpx.HTTPError:
                failed_items.append((payload_time, payload))
        for item in failed_items:
            self._requests_data_queue.put_nowait(item)

    @retry(raise_on_giveup=False)
    async def _send_app_info(self, client: httpx.AsyncClient, payload: Dict[str, Any]) -> None:
        logger.debug("Sending app info")
        response = await client.post(url="/info", json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            self.stop_sync_loop()
            logger.error(f"Invalid Apitally client ID {self.client_id}")
        else:
            response.raise_for_status()
        self._app_info_sent = True
        self._app_info_payload = None

    @retry()
    async def _send_requests_data(self, client: httpx.AsyncClient, payload: Dict[str, Any]) -> None:
        logger.debug("Sending requests data")
        response = await client.post(url="/requests", json=payload)
        response.raise_for_status()
