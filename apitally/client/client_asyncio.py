from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import suppress
from functools import partial
from typing import Any, AsyncIterator, Dict, Optional, Union
from uuid import UUID

import backoff
import httpx

from apitally.client.client_base import MAX_QUEUE_TIME, REQUEST_TIMEOUT, ApitallyClientBase
from apitally.client.logging import get_logger
from apitally.client.request_logging import RequestLoggingConfig


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
    def __init__(
        self,
        client_id: str,
        env: str,
        request_logging_config: Optional[RequestLoggingConfig] = None,
        proxy: Optional[Union[str, httpx.Proxy]] = None,
    ) -> None:
        super().__init__(client_id=client_id, env=env, request_logging_config=request_logging_config)
        self.proxy = proxy
        self._stop_sync_loop = False
        self._sync_loop_task: Optional[asyncio.Task] = None
        self._sync_data_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._set_startup_data_task: Optional[asyncio.Task] = None

    def get_http_client(self) -> httpx.AsyncClient:
        if httpx.__version__ >= "0.26.0":
            # `proxy` parameter was added in version 0.26.0
            return httpx.AsyncClient(base_url=self.hub_url, timeout=REQUEST_TIMEOUT, proxy=self.proxy)
        else:
            return httpx.AsyncClient(base_url=self.hub_url, timeout=REQUEST_TIMEOUT, proxies=self.proxy)

    def start_sync_loop(self) -> None:
        self._stop_sync_loop = False
        self._sync_loop_task = asyncio.create_task(self._run_sync_loop())

    async def _run_sync_loop(self) -> None:
        last_sync_time = 0.0
        while not self._stop_sync_loop:
            try:
                self.request_logger.write_to_file()
            except Exception:  # pragma: no cover
                logger.exception("An error occurred while writing request logs")

            now = time.time()
            if (now - last_sync_time) >= self.sync_interval:
                try:
                    async with self.get_http_client() as client:
                        tasks = [self.send_sync_data(client), self.send_log_data(client)]
                        if not self._startup_data_sent and last_sync_time > 0:  # not on first sync
                            tasks.append(self.send_startup_data(client))
                        await asyncio.gather(*tasks)
                    last_sync_time = now
                except Exception:  # pragma: no cover
                    logger.exception("An error occurred during sync with Apitally hub")

            self.request_logger.maintain()
            await asyncio.sleep(1)

    def stop_sync_loop(self) -> None:
        self._stop_sync_loop = True

    async def handle_shutdown(self) -> None:
        self.enabled = False
        if self._sync_loop_task is not None:
            self._sync_loop_task.cancel()
        # Send any remaining data before exiting
        async with self.get_http_client() as client:
            await self.send_sync_data(client)
            await self.send_log_data(client)

    def set_startup_data(self, data: Dict[str, Any]) -> None:
        self._startup_data_sent = False
        self._startup_data = self.add_uuids_to_data(data)
        self._set_startup_data_task = asyncio.create_task(self._set_startup_data())

    async def _set_startup_data(self) -> None:
        async with self.get_http_client() as client:
            await self.send_startup_data(client)

    async def send_startup_data(self, client: httpx.AsyncClient) -> None:
        if self._startup_data is not None:
            await self._send_startup_data(client, self._startup_data)

    async def send_sync_data(self, client: httpx.AsyncClient) -> None:
        data = self.get_sync_data()
        self._sync_data_queue.put_nowait(data)

        i = 0
        while not self._sync_data_queue.empty():
            data = self._sync_data_queue.get_nowait()
            try:
                if time.time() - data["timestamp"] <= MAX_QUEUE_TIME:
                    if i > 0:
                        await asyncio.sleep(random.uniform(0.1, 0.5))
                    await self._send_sync_data(client, data)
                    i += 1
            except httpx.HTTPError:
                self._sync_data_queue.put_nowait(data)
                break
            finally:
                self._sync_data_queue.task_done()

    async def send_log_data(self, client: httpx.AsyncClient) -> None:
        self.request_logger.rotate_file()
        i = 0
        while log_file := self.request_logger.get_file():
            if i > 0:
                time.sleep(random.uniform(0.1, 0.3))
            try:
                stream = log_file.stream_lines_compressed()
                await self._send_log_data(client, log_file.uuid, stream)
                log_file.delete()
            except httpx.HTTPError:
                self.request_logger.retry_file_later(log_file)
                break
            i += 1
            if i >= 10:
                break

    @retry(raise_on_giveup=False)
    async def _send_startup_data(self, client: httpx.AsyncClient, data: Dict[str, Any]) -> None:
        logger.debug("Sending startup data to Apitally hub")
        response = await client.post(url="/startup", json=data)
        self._handle_hub_response(response)
        self._startup_data_sent = True
        self._startup_data = None

    @retry()
    async def _send_sync_data(self, client: httpx.AsyncClient, data: Dict[str, Any]) -> None:
        logger.debug("Synchronizing data with Apitally hub")
        response = await client.post(url="/sync", json=data)
        self._handle_hub_response(response)

    async def _send_log_data(self, client: httpx.AsyncClient, uuid: UUID, stream: AsyncIterator[bytes]) -> None:
        logger.debug("Streaming request log data to Apitally hub")
        response = await client.post(url=f"{self.hub_url}/log?uuid={uuid}", content=stream)
        if response.status_code == 402 and "Retry-After" in response.headers:
            with suppress(ValueError):
                retry_after = int(response.headers["Retry-After"])
                self.request_logger.suspend_until = time.time() + retry_after
                self.request_logger.clear()
                return
        self._handle_hub_response(response)

    def _handle_hub_response(self, response: httpx.Response) -> None:
        if response.status_code == 404:
            self.enabled = False
            self.stop_sync_loop()
            logger.error("Invalid Apitally client ID: %s", self.client_id)
        elif response.status_code == 422:
            logger.error("Received validation error from Apitally hub: %s", response.json())
        else:
            response.raise_for_status()
