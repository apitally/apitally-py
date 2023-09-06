from __future__ import annotations

import logging
import sys
import time
from threading import Event, Thread
from typing import Any, Callable, Dict, Optional

import backoff
import requests

from apitally.client.base import ApitallyClientBase, handle_retry_giveup


logger = logging.getLogger(__name__)
retry = backoff.on_exception(
    backoff.expo,
    requests.RequestException,
    max_tries=3,
    on_giveup=handle_retry_giveup,
    raise_on_giveup=False,
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
    from atexit import register as register_exit


class ApitallyClient(ApitallyClientBase):
    def __init__(self, client_id: str, env: str, sync_api_keys: bool = False, sync_interval: float = 60) -> None:
        super().__init__(client_id=client_id, env=env, sync_api_keys=sync_api_keys, sync_interval=sync_interval)
        self._thread: Optional[Thread] = None
        self._stop_sync_loop = Event()

    def start_sync_loop(self) -> None:
        self._stop_sync_loop.clear()
        if self._thread is None or not self._thread.is_alive():
            self._thread = Thread(target=self._run_sync_loop)
            self._thread.start()
            register_exit(self.stop_sync_loop)

    def _run_sync_loop(self) -> None:
        if self.sync_api_keys:
            with requests.Session() as session:
                self.get_keys(session)
        while not self._stop_sync_loop.is_set():
            try:
                time.sleep(self.sync_interval)
                with requests.Session() as session:
                    self.send_requests_data(session)
                    if self.sync_api_keys:
                        self.get_keys(session)
            except Exception as e:  # pragma: no cover
                logger.exception(e)

    def stop_sync_loop(self) -> None:
        self._stop_sync_loop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def send_app_info(self, app_info: Dict[str, Any]) -> None:
        payload = self.get_info_payload(app_info)
        self._send_app_info(payload=payload)

    def send_requests_data(self, session: requests.Session) -> None:
        payload = self.get_requests_payload()
        self._send_requests_data(session, payload)

    def get_keys(self, session: requests.Session) -> None:
        if response_data := self._get_keys(session):  # Response data can be None if backoff gives up
            self.handle_keys_response(response_data)
        elif self.key_registry.salt is None:
            logger.error("Initial Apitally API key sync failed")
            # Exit because the application will not be able to authenticate requests
            sys.exit(1)

    @retry
    def _send_app_info(self, payload: Dict[str, Any]) -> None:
        response = requests.post(url=f"{self.hub_url}/info", json=payload)
        if response.status_code == 404 and "Client ID" in response.text:
            self.stop_sync_loop()
            logger.error(f"Invalid Apitally client ID {self.client_id}")
        else:
            response.raise_for_status()

    @retry
    def _send_requests_data(self, session: requests.Session, payload: Dict[str, Any]) -> None:
        response = session.post(url=f"{self.hub_url}/requests", json=payload)
        response.raise_for_status()

    @retry
    def _get_keys(self, session: requests.Session) -> Dict[str, Any]:
        response = session.get(url=f"{self.hub_url}/keys")
        response.raise_for_status()
        return response.json()
