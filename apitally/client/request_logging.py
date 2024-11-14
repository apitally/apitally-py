import gzip
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from io import BufferedReader
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, TypedDict, Union


MAX_FILE_SIZE = 2_000_000  # 2 MB (compressed)


@dataclass
class RequestLoggingConfig:
    """
    Configuration for request logging.

    Attributes:
        enabled: Whether request logging is enabled
        include_query_params: Whether to include query parameter values
        include_request_headers: Whether to include request header values
        include_request_body: Whether to include the request body
        include_response_headers: Whether to include response header values
        include_response_body: Whether to include the response body (only plain text or JSON)
    """

    enabled: bool = False
    include_query_params: bool = True
    include_request_headers: bool = False
    include_request_body: bool = False
    include_response_headers: bool = True
    include_response_body: bool = False
    mask_query_params: Union[List[str], Callable[[str, str], Optional[bool]], None] = None
    mask_headers: Union[List[str], Callable[[str, str], Optional[bool]], None] = None
    mask_body: Union[List[str], Callable[[str, str], Optional[bool]], None] = None


class RequestDict(TypedDict):
    method: str
    path: str
    url: str
    headers: Dict[str, str]


class ResponseDict(TypedDict):
    status_code: int
    response_time: float
    headers: Dict[str, str]
    size: int | None


class TempGzipFile:
    def __init__(self) -> None:
        self.file = tempfile.NamedTemporaryFile(
            suffix=".gz",
            prefix="apitally-",
            delete=False,
        )
        self.gzip_file = gzip.open(self.file, "wb")

    @property
    def path(self) -> Path:
        return Path(self.file.name)

    @property
    def size(self) -> int:
        return self.file.tell()

    def write_line(self, data: bytes) -> None:
        self.gzip_file.write(data + b"\n")

    def open_compressed(self) -> BufferedReader:
        return open(self.path, "rb")

    def stream_lines_compressed(self) -> Iterator[bytes]:
        with open(self.path, "rb") as fp:
            for line in fp:
                yield line

    def close(self) -> None:
        self.gzip_file.close()
        self.file.close()

    def delete(self) -> None:
        self.close()
        self.path.unlink(missing_ok=True)


class RequestLogger:
    def __init__(self, config: Optional[RequestLoggingConfig]) -> None:
        self.config = config or RequestLoggingConfig()
        self.enabled = self.config.enabled and self._check_writable_fs()
        self.serialize = self._get_json_serializer()
        self.write_deque: deque[bytes] = deque([], 1000)
        self.file_deque: deque[TempGzipFile] = deque([])
        self.file: Optional[TempGzipFile] = None
        self.lock = threading.Lock()

    @property
    def current_file_size(self) -> int:
        return self.file.size if self.file is not None else 0

    def log_request(self, request: RequestDict, response: ResponseDict) -> None:
        if not self.enabled:
            return
        item = {
            "time_ns": time.time_ns() - response["response_time"] * 1_000_000_000,
            "request": request,
            "response": response,
        }
        serialized_item = self.serialize(item)
        self.write_deque.append(serialized_item)

    def write_to_file(self) -> None:
        if not self.enabled or len(self.write_deque) == 0:
            return
        with self.lock:
            if self.file is None:
                self.file = TempGzipFile()
            while True:
                try:
                    item = self.write_deque.popleft()
                    self.file.write_line(item)
                except IndexError:
                    break

    def get_file(self) -> Optional[TempGzipFile]:
        try:
            return self.file_deque.popleft()
        except IndexError:
            return None

    def retry_file_later(self, file: TempGzipFile) -> None:
        self.file_deque.appendleft(file)

    def rotate_file(self) -> None:
        if self.file is not None:
            with self.lock:
                self.file.close()
                self.file_deque.append(self.file)
                self.file = None

    def maybe_rotate_file(self) -> None:
        if self.current_file_size > MAX_FILE_SIZE:
            self.rotate_file()
        while len(self.file_deque) > 50:
            file = self.file_deque.popleft()
            file.delete()

    def close(self) -> None:
        self.enabled = False
        self.rotate_file()
        for file in self.file_deque:
            file.delete()

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
                return json.dumps(obj, separators=(",", ":")).encode()

            return json_dumps
