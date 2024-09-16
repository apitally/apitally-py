from __future__ import annotations

import logging
import queue
import random
import time
from functools import partial
from threading import Event, Thread
from typing import Any, Callable, Dict, Optional, Tuple

import backoff
import requests

from apitally.client.base import MAX_QUEUE_TIME, REQUEST_TIMEOUT, ApitallyClientBase
from apitally.client.logging import get_logger


logger = get_logger(__name__)
retry = partial(
    backoff.on_exception,
    backoff.expo,
    requests.RequestException,
    max_tries=3,
    logger=logger,
    giveup_log_level=logging.WARNING,
)


# Function to register an on-exit callback for both Python and IPython runtimes
try:

    def register_exit(func: Callable[..., Any], *args, **kwargs) -> Callable[..., Any]:  # pragma: no cover
        def callback():
            func()
            ipython.events.unregister("post_execute", callback)

        ipython.events.register("post_execute", callback)
        return func

    ipython = get_ipython()  # type: ignore
except NameError:
    from atexit import register as register_exit  # type: ignore[assignment]


class ApitallyClient(ApitallyClientBase):
    def __init__(self, client_id: str, env: str) -> None:
        super().__init__(client_id=client_id, env=env)
        self._thread: Optional[Thread] = None
        self._stop_sync_loop = Event()
        self._sync_data_queue: queue.Queue[Tuple[float, Dict[str, Any]]] = queue.Queue()

    def start_sync_loop(self) -> None:
        self._stop_sync_loop.clear()
        if self._thread is None or not self._thread.is_alive():
            self._thread = Thread(target=self._run_sync_loop, daemon=True)
            self._thread.start()
            register_exit(self.stop_sync_loop)

    def _run_sync_loop(self) -> None:
        try:
            last_sync_time = 0.0
            while not self._stop_sync_loop.is_set():
                try:
                    now = time.time()
                    if (now - last_sync_time) >= self.sync_interval:
                        with requests.Session() as session:
                            if not self._startup_data_sent and last_sync_time > 0:  # not on first sync
                                self.send_startup_data(session)
                            self.send_sync_data(session)
                        last_sync_time = now
                    time.sleep(1)
                except Exception:  # pragma: no cover
                    logger.exception("An error occurred during sync with Apitally hub")
        finally:
            # Send any remaining data before exiting
            with requests.Session() as session:
                self.send_sync_data(session)

    def stop_sync_loop(self) -> None:
        self._stop_sync_loop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def set_startup_data(self, data: Dict[str, Any]) -> None:
        self._startup_data_sent = False
        self._startup_data = self.add_uuids_to_data(data)
        with requests.Session() as session:
            self.send_startup_data(session)

    def send_startup_data(self, session: requests.Session) -> None:
        if self._startup_data is not None:
            self._send_startup_data(session, self._startup_data)

    def send_sync_data(self, session: requests.Session) -> None:
        data = self.get_sync_data()
        self._sync_data_queue.put_nowait((time.time(), data))

        i = 0
        while not self._sync_data_queue.empty():
            timestamp, data = self._sync_data_queue.get_nowait()
            try:
                if (time_offset := time.time() - timestamp) <= MAX_QUEUE_TIME:
                    if i > 0:
                        time.sleep(random.uniform(0.1, 0.3))
                    data["time_offset"] = time_offset
                    self._send_sync_data(session, data)
                    i += 1
            except requests.RequestException:
                self._sync_data_queue.put_nowait((timestamp, data))
                break
            finally:
                self._sync_data_queue.task_done()

    @retry(raise_on_giveup=False)
    def _send_startup_data(self, session: requests.Session, data: Dict[str, Any]) -> None:
        logger.debug("Sending startup data to Apitally hub")
        response = session.post(url=f"{self.hub_url}/startup", json=data, timeout=REQUEST_TIMEOUT)
        self._handle_hub_response(response)
        self._startup_data_sent = True
        self._startup_data = None

    @retry()
    def _send_sync_data(self, session: requests.Session, data: Dict[str, Any]) -> None:
        logger.debug("Synchronizing data with Apitally hub")
        response = session.post(url=f"{self.hub_url}/sync", json=data, timeout=REQUEST_TIMEOUT)
        self._handle_hub_response(response)

    def _handle_hub_response(self, response: requests.Response) -> None:
        if response.status_code == 404:
            self.stop_sync_loop()
            logger.error("Invalid Apitally client ID: %s", self.client_id)
        elif response.status_code == 422:
            logger.error("Received validation error from Apitally hub: %s", response.json())
        else:
            response.raise_for_status()
