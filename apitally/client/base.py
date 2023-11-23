from __future__ import annotations

import json
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import scrypt
from math import floor
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar, Union, cast
from uuid import UUID, uuid4

from apitally.client.logging import get_logger


logger = get_logger(__name__)

HUB_BASE_URL = os.getenv("APITALLY_HUB_BASE_URL") or "https://hub.apitally.io"
HUB_VERSION = "v1"
REQUEST_TIMEOUT = 10
MAX_QUEUE_TIME = 3600

TApitallyClient = TypeVar("TApitallyClient", bound="ApitallyClientBase")


class ApitallyClientBase:
    _instance: Optional[ApitallyClientBase] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs) -> ApitallyClientBase:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        client_id: str,
        env: str,
        sync_api_keys: bool = False,
        sync_interval: float = 60,
        key_cache_class: Optional[Type[ApitallyKeyCacheBase]] = None,
    ) -> None:
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
        self.sync_api_keys = sync_api_keys
        self.sync_interval = sync_interval
        self.instance_uuid = str(uuid4())
        self.request_logger = RequestLogger()
        self.validation_error_logger = ValidationErrorLogger()
        self.key_registry = KeyRegistry()
        self.key_cache = key_cache_class(client_id=client_id, env=env) if key_cache_class is not None else None

        self._app_info_payload: Optional[Dict[str, Any]] = None
        self._app_info_sent = False
        self._started_at = time.time()
        self._keys_updated_at: Optional[float] = None

        if self.key_cache is not None and (key_data := self.key_cache.retrieve()):
            try:
                self.handle_keys_response(json.loads(key_data), cache=False)
            except (json.JSONDecodeError, TypeError, KeyError):  # pragma: no cover
                logger.exception("Failed to load API keys from cache")

    @classmethod
    def get_instance(cls: Type[TApitallyClient]) -> TApitallyClient:
        if cls._instance is None:
            raise RuntimeError("Apitally client not initialized")  # pragma: no cover
        return cast(TApitallyClient, cls._instance)

    @property
    def hub_url(self) -> str:
        return f"{HUB_BASE_URL}/{HUB_VERSION}/{self.client_id}/{self.env}"

    def get_info_payload(self, app_info: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "instance_uuid": self.instance_uuid,
            "message_uuid": str(uuid4()),
        }
        payload.update(app_info)
        return payload

    def get_requests_payload(self) -> Dict[str, Any]:
        requests = self.request_logger.get_and_reset_requests()
        validation_errors = self.validation_error_logger.get_and_reset_validation_errors()
        api_key_usage = self.key_registry.get_and_reset_usage_counts() if self.sync_api_keys else {}
        return {
            "instance_uuid": self.instance_uuid,
            "message_uuid": str(uuid4()),
            "requests": requests,
            "validation_errors": validation_errors,
            "api_key_usage": api_key_usage,
        }

    def handle_keys_response(self, response_data: Dict[str, Any], cache: bool = True) -> None:
        self.key_registry.salt = response_data["salt"]
        self.key_registry.update(response_data["keys"])

        if cache and self.key_cache is not None:
            self.key_cache.store(json.dumps(response_data, check_circular=False, allow_nan=False))


class ApitallyKeyCacheBase(ABC):
    def __init__(self, client_id: str, env: str) -> None:
        self.client_id = client_id
        self.env = env

    @property
    def cache_key(self) -> str:
        return f"apitally:keys:{self.client_id}:{self.env}"

    @abstractmethod
    def store(self, data: str) -> None:
        """Store the key data in cache as a JSON string."""
        pass  # pragma: no cover

    @abstractmethod
    def retrieve(self) -> str | bytes | bytearray | None:
        """Retrieve the stored key data from the cache as a JSON string."""
        pass  # pragma: no cover


@dataclass(frozen=True)
class RequestInfo:
    consumer: Optional[str]
    method: str
    path: str
    status_code: int


class RequestLogger:
    def __init__(self) -> None:
        self.request_counts: Counter[RequestInfo] = Counter()
        self.response_times: Dict[RequestInfo, Counter[int]] = {}
        self._lock = threading.Lock()

    def log_request(
        self, consumer: Optional[str], method: str, path: str, status_code: int, response_time: float
    ) -> None:
        request_info = RequestInfo(
            consumer=consumer,
            method=method.upper(),
            path=path,
            status_code=status_code,
        )
        response_time_ms_bin = int(floor(response_time / 0.01) * 10)  # In ms, rounded down to nearest 10ms
        with self._lock:
            self.request_counts[request_info] += 1
            self.response_times.setdefault(request_info, Counter())[response_time_ms_bin] += 1

    def get_and_reset_requests(self) -> List[Dict[str, Any]]:
        data: List[Dict[str, Any]] = []
        with self._lock:
            for request_info, count in self.request_counts.items():
                data.append(
                    {
                        "consumer": request_info.consumer,
                        "method": request_info.method,
                        "path": request_info.path,
                        "status_code": request_info.status_code,
                        "request_count": count,
                        "response_times": self.response_times.get(request_info) or Counter(),
                    }
                )
            self.request_counts.clear()
            self.response_times.clear()
        return data


@dataclass(frozen=True)
class ValidationError:
    consumer: Optional[str]
    method: str
    path: str
    loc: Tuple[str, ...]
    msg: str
    type: str


class ValidationErrorLogger:
    def __init__(self) -> None:
        self.error_counts: Counter[ValidationError] = Counter()
        self._lock = threading.Lock()

    def log_validation_errors(
        self, consumer: Optional[str], method: str, path: str, detail: List[Dict[str, Any]]
    ) -> None:
        with self._lock:
            for error in detail:
                try:
                    validation_error = ValidationError(
                        consumer=consumer,
                        method=method.upper(),
                        path=path,
                        loc=tuple(str(loc) for loc in error["loc"]),
                        type=error["type"],
                        msg=error["msg"],
                    )
                    self.error_counts[validation_error] += 1
                except (KeyError, TypeError):  # pragma: no cover
                    pass

    def get_and_reset_validation_errors(self) -> List[Dict[str, Any]]:
        data: List[Dict[str, Any]] = []
        with self._lock:
            for validation_error, count in self.error_counts.items():
                data.append(
                    {
                        "consumer": validation_error.consumer,
                        "method": validation_error.method,
                        "path": validation_error.path,
                        "loc": validation_error.loc,
                        "msg": validation_error.msg,
                        "type": validation_error.type,
                        "error_count": count,
                    }
                )
            self.error_counts.clear()
        return data


@dataclass(frozen=True)
class KeyInfo:
    key_id: int
    api_key_id: int
    name: str = ""
    scopes: List[str] = field(default_factory=list)
    expires_at: Optional[datetime] = None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at < datetime.now()

    def has_scopes(self, scopes: Union[List[str], str]) -> bool:
        if isinstance(scopes, str):
            scopes = [scopes]
        if not isinstance(scopes, list):
            raise ValueError("scopes must be a string or a list of strings")
        return all(scope in self.scopes for scope in scopes)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> KeyInfo:
        return cls(
            key_id=data["key_id"],
            api_key_id=data["api_key_id"],
            name=data.get("name", ""),
            scopes=data.get("scopes", []),
            expires_at=(
                datetime.now() + timedelta(seconds=data["expires_in_seconds"])
                if data["expires_in_seconds"] is not None
                else None
            ),
        )


class KeyRegistry:
    def __init__(self) -> None:
        self.salt: Optional[str] = None
        self.keys: Dict[str, KeyInfo] = {}
        self.usage_counts: Counter[int] = Counter()
        self._lock = threading.Lock()

    def get(self, api_key: str) -> Optional[KeyInfo]:
        hash = self.hash_api_key(api_key.strip())
        with self._lock:
            key = self.keys.get(hash)
            if key is None or key.is_expired:
                return None
            self.usage_counts[key.api_key_id] += 1
        return key

    def hash_api_key(self, api_key: str) -> str:
        if self.salt is None:
            raise RuntimeError("Apitally API keys not initialized")
        return scrypt(api_key.encode(), salt=bytes.fromhex(self.salt), n=256, r=4, p=1, dklen=32).hex()

    def update(self, keys: Dict[str, Dict[str, Any]]) -> None:
        with self._lock:
            self.keys = {hash: KeyInfo.from_dict(data) for hash, data in keys.items()}

    def get_and_reset_usage_counts(self) -> Dict[int, int]:
        with self._lock:
            data = dict(self.usage_counts)
            self.usage_counts.clear()
        return data
