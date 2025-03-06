from __future__ import annotations

import os
import re
import threading
import time
from abc import ABC
from typing import Any, Dict, Optional, Type, TypeVar, cast
from uuid import UUID, uuid4

from apitally.client.consumers import ConsumerRegistry
from apitally.client.logging import get_logger
from apitally.client.request_logging import RequestLogger, RequestLoggingConfig
from apitally.client.requests import RequestCounter
from apitally.client.server_errors import ServerErrorCounter
from apitally.client.validation_errors import ValidationErrorCounter


logger = get_logger(__name__)

HUB_BASE_URL = os.getenv("APITALLY_HUB_BASE_URL") or "https://hub.apitally.io"
HUB_VERSION = "v2"
REQUEST_TIMEOUT = 10
MAX_QUEUE_TIME = 3600
SYNC_INTERVAL = 60
INITIAL_SYNC_INTERVAL = 10
INITIAL_SYNC_INTERVAL_DURATION = 3600

TApitallyClient = TypeVar("TApitallyClient", bound="ApitallyClientBase")


class ApitallyClientBase(ABC):
    _instance: Optional[ApitallyClientBase] = None
    _lock = threading.Lock()

    def __new__(cls: Type[TApitallyClient], *args, **kwargs) -> TApitallyClient:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cast(TApitallyClient, cls._instance)

    def __init__(self, client_id: str, env: str, request_logging_config: Optional[RequestLoggingConfig] = None) -> None:
        if hasattr(self, "client_id"):
            raise RuntimeError("Apitally client is already initialized")  # pragma: no cover
        try:
            UUID(client_id)
        except ValueError:
            raise ValueError(f"invalid client_id '{client_id}' (expecting hexadecimal UUID format)")
        if re.match(r"^[\w-]{1,32}$", env) is None:
            raise ValueError(f"invalid env '{env}' (expecting 1-32 alphanumeric lowercase characters and hyphens only)")

        self.client_id = client_id
        self.env = env
        self.enabled = True
        self.instance_uuid = str(uuid4())
        self.request_counter = RequestCounter()
        self.validation_error_counter = ValidationErrorCounter()
        self.server_error_counter = ServerErrorCounter()
        self.consumer_registry = ConsumerRegistry()
        self.request_logger = RequestLogger(request_logging_config)

        self._startup_data: Optional[Dict[str, Any]] = None
        self._startup_data_sent = False
        self._started_at = time.time()

    @classmethod
    def get_instance(cls: Type[TApitallyClient]) -> TApitallyClient:
        if cls._instance is None:
            raise RuntimeError("Apitally client not initialized")  # pragma: no cover
        return cast(TApitallyClient, cls._instance)

    @property
    def sync_interval(self) -> float:
        return (
            SYNC_INTERVAL if time.time() - self._started_at > INITIAL_SYNC_INTERVAL_DURATION else INITIAL_SYNC_INTERVAL
        )

    @property
    def hub_url(self) -> str:
        return f"{HUB_BASE_URL}/{HUB_VERSION}/{self.client_id}/{self.env}"

    def add_uuids_to_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        data_with_uuids = {
            "instance_uuid": self.instance_uuid,
            "message_uuid": str(uuid4()),
        }
        data_with_uuids.update(data)
        return data_with_uuids

    def get_sync_data(self) -> Dict[str, Any]:
        data = {
            "timestamp": time.time(),
            "requests": self.request_counter.get_and_reset_requests(),
            "validation_errors": self.validation_error_counter.get_and_reset_validation_errors(),
            "server_errors": self.server_error_counter.get_and_reset_server_errors(),
            "consumers": self.consumer_registry.get_and_reset_updated_consumers(),
        }
        return self.add_uuids_to_data(data)
