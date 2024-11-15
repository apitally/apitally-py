import base64
import gzip
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from io import BufferedReader
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple, TypedDict, Union
from urllib.parse import urlparse, urlunparse

from apitally.client.logging import get_logger


logger = get_logger(__name__)

MAX_BODY_SIZE = 100_000  # 100 KB (uncompressed)
MAX_FILE_SIZE = 2_000_000  # 2 MB (compressed)
REQUEST_BODY_TOO_LARGE = b"<Request body too large>"
RESPONSE_BODY_TOO_LARGE = b"<Response body too large>"


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
    path: Optional[str]
    url: str
    headers: List[Tuple[str, str]]
    size: Optional[int]
    consumer: Optional[str]
    body: Optional[bytes]


class ResponseDict(TypedDict):
    status_code: int
    response_time: float
    headers: List[Tuple[str, str]]
    size: Optional[int]
    body: Optional[bytes]


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
        self.enabled = self.config.enabled and _check_writable_fs()
        self.serialize = _get_json_serializer()
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

        if not self.config.include_query_params:
            request["url"] = _strip_query_params(request["url"])
        if not self.config.include_request_headers:
            request["headers"] = []
        if not self.config.include_request_body:
            request["body"] = None
        if not self.config.include_response_headers:
            response["headers"] = []
        if not self.config.include_response_body:
            response["body"] = None

        item = {
            "time_ns": time.time_ns() - response["response_time"] * 1_000_000_000,
            "request": _skip_empty_values(request),
            "response": _skip_empty_values(response),
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


def _check_writable_fs():
    try:
        with tempfile.TemporaryFile():
            return True
    except (IOError, OSError):
        logger.error("Unable to create temporary file for request logging")
        return False


def _get_json_serializer() -> Callable[[Any], bytes]:
    def default(obj: Any) -> Any:
        if isinstance(obj, bytes):
            return base64.b64encode(obj).decode()
        raise TypeError

    try:
        import orjson  # type: ignore

        def orjson_dumps(obj: Any) -> bytes:
            return orjson.dumps(obj, default=default)

        return orjson_dumps
    except ImportError:
        import json

        def json_dumps(obj: Any) -> bytes:
            return json.dumps(obj, separators=(",", ":"), default=default).encode()

        return json_dumps


def _strip_query_params(url: str) -> str:
    parsed = urlparse(url)
    stripped = parsed._replace(query="")
    return urlunparse(stripped)


def _skip_empty_values(data: Mapping) -> Dict:
    return {
        k: v for k, v in data.items() if v is not None and not (isinstance(v, (list, dict, bytes, str)) and len(v) == 0)
    }
