from __future__ import annotations

import os
import re
import threading
import time
from abc import ABC
from typing import Any, Optional, Type, TypeVar, cast
from uuid import UUID, uuid4

from apitally.client.consumers import ConsumerRegistry
from apitally.client.instance import get_or_create_instance_uuid
from apitally.client.logging import get_logger
from apitally.client.request_logging import RequestLogger, RequestLoggingConfig
from apitally.client.requests import RequestCounter
from apitally.client.resources import get_cpu_memory_usage
from apitally.client.server_errors import ServerErrorCounter
from apitally.client.spans import SpanCollector
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

        self.client_id = str(client_id)
        self.env = str(env)
        self.enabled = True

        if not self.validate_client_id(self.client_id):
            self.enabled = False
            logger.error(f"invalid client_id '{self.client_id}' (expecting string in hexadecimal UUID format)")
        if not self.validate_env(self.env):
            self.enabled = False
            logger.error(
                f"invalid env '{self.env}' (expecting string with 1-32 alphanumeric characters and hyphens only)"
            )

        self.instance_uuid, self._instance_lock_fd = get_or_create_instance_uuid(self.client_id, self.env)
        self.request_counter = RequestCounter()
        self.validation_error_counter = ValidationErrorCounter()
        self.server_error_counter = ServerErrorCounter()
        self.consumer_registry = ConsumerRegistry()
        self.request_logger = RequestLogger(request_logging_config)
        self.span_collector = SpanCollector(
            enabled=self.enabled and self.request_logger.enabled and self.request_logger.config.capture_traces
        )

        self._startup_data: Optional[dict[str, Any]] = None
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

    def add_uuids_to_data(self, data: dict[str, Any]) -> dict[str, Any]:
        data_with_uuids = {
            "instance_uuid": self.instance_uuid,
            "message_uuid": str(uuid4()),
        }
        data_with_uuids.update(data)
        return data_with_uuids

    def get_sync_data(self) -> dict[str, Any]:
        data = {
            "timestamp": time.time(),
            "requests": self.request_counter.get_and_reset_requests(),
            "validation_errors": self.validation_error_counter.get_and_reset_validation_errors(),
            "server_errors": self.server_error_counter.get_and_reset_server_errors(),
            "consumers": self.consumer_registry.get_and_reset_updated_consumers(),
            "resources": get_cpu_memory_usage(),
        }
        return self.add_uuids_to_data(data)

    @staticmethod
    def validate_client_id(client_id: str) -> bool:
        try:
            UUID(client_id)
            return True
        except ValueError:
            return False

    @staticmethod
    def validate_env(env: str) -> bool:
        return re.match(r"^[\w-]{1,32}$", env) is not None
