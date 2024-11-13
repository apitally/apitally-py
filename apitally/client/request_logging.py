import shutil
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from queue import Full, Queue
from typing import Any, Callable, Dict, List, TypedDict


@dataclass
class RequestLoggingConfig:
    """
    Configuration for request logging.

    Attributes:
        enabled: Whether request logging is enabled
        include_headers: Whether to include headers in logs
    """

    enabled: bool = True
    include_headers: bool = False
    include_cookies: bool = False
    mask_query_params: bool | List[str] | Callable[[str, str], bool] = False
    mask_headers: bool | List[str] | Callable[[str, str], bool] = False
    mask_cookies: bool | List[str] | Callable[[str, str], bool] = False
    disable_default_masking: bool = False


class RequestDict(TypedDict):
    method: str
    path: str
    url: str
    headers: Dict[str, str]
    cookies: Dict[str, str]


class ResponseDict(TypedDict):
    status_code: int
    response_time: float
    headers: Dict[str, str]
    size: int | None


class RequestLogger:
    def __init__(self, config: RequestLoggingConfig) -> None:
        self.config = config
        self.writable_fs = self._check_writable_fs()
        self.serialize = self._get_json_serializer()
        self.write_queue: Queue[bytes] = Queue(1000)
        self.temp_dir = Path(tempfile.mkdtemp(prefix="apitally-"))

    def log_request(self, request: RequestDict, response: ResponseDict) -> None:
        item = {
            "timestamp": time.time(),
            "request": request,
            "response": response,
        }
        serialized_item = self.serialize(item)
        with suppress(Full):
            self.write_queue.put(serialized_item, block=False)

    def write_to_file(self) -> None:
        pass

    def delete_temp_dir(self) -> None:
        if self.writable_fs and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    @staticmethod
    def _check_writable_fs():
        try:
            with tempfile.TemporaryFile():
                return True
        except (IOError, OSError):
            # TODO: Log warning that request logging is using memory instead of disk
            return False

    @staticmethod
    def _get_json_serializer() -> Callable[[Any], bytes]:
        try:
            import orjson  # type: ignore

            def orjson_dumps(obj: Any) -> bytes:
                return orjson.dumps(obj)

            return orjson_dumps
        except ImportError:
            import json

            def json_dumps(obj: Any) -> bytes:
                return json.dumps(obj).encode("utf-8")

            return json_dumps
