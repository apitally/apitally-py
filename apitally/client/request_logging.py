import gzip
import queue
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, TypedDict


@dataclass
class RequestLoggingConfig:
    """
    Configuration for request logging.

    Attributes:
        enabled: Whether request logging is enabled
        include_request_headers: Whether to include request headers
        include_request_body: Whether to include the request body
        include_response_headers: Whether to include response headers
        include_response_body: Whether to include the response body (only plain text or JSON)
    """

    enabled: bool = True
    include_request_headers: bool = False
    include_request_body: bool = False
    include_response_headers: bool = True
    include_response_body: bool = False
    mask_query_params: bool | List[str] | Callable[[str, str], bool] = False
    mask_headers: bool | List[str] | Callable[[str, str], bool] = False
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


class TempGzipFile:
    def __init__(self) -> None:
        self.file = tempfile.NamedTemporaryFile(
            suffix=".gz",
            prefix="apitally-",
            delete=False,
        )
        self.gzip_file = gzip.open(self.file, "ab")

    @property
    def path(self) -> Path:
        return Path(self.file.name)

    def write_line(self, data: bytes) -> None:
        self.gzip_file.write(data + b"\n")

    def stream_lines_compressed(self) -> Iterator[bytes]:
        self.close()
        with open(self.path, "rb") as fp:
            for line in fp:
                yield line

    def close(self) -> None:
        self.gzip_file.close()
        self.file.close()

    def delete(self) -> None:
        self.close()
        self.path.unlink()


class RequestLogger:
    def __init__(self, config: RequestLoggingConfig) -> None:
        self.config = config
        self.enabled = self.config.enabled and self._check_writable_fs()
        self.serialize = self._get_json_serializer()
        self.write_queue: queue.Queue[bytes] = queue.Queue(1000)
        self.file_send_queue: queue.Queue[TempGzipFile] = queue.Queue(100)
        self.file: Optional[TempGzipFile] = None
        self.lock = threading.Lock()

    def log_request(self, request: RequestDict, response: ResponseDict) -> None:
        if not self.enabled:
            return
        item = {
            "timestamp": time.time(),
            "request": request,
            "response": response,
        }
        serialized_item = self.serialize(item)
        with suppress(queue.Full):
            self.write_queue.put(serialized_item, block=False)

    def write_to_file(self) -> None:
        if not self.enabled:
            return
        with self.lock:
            if self.file is None:
                self.file = TempGzipFile()
            while True:
                try:
                    item = self.write_queue.get_nowait()
                    self.file.write_line(item)
                except queue.Empty:
                    break

    def get_streamer(self) -> Optional[Callable[[], Iterator[bytes]]]:
        try:
            temp_file = self.file_send_queue.get_nowait()

            def streamer() -> Iterator[bytes]:
                completed = False
                try:
                    yield from temp_file.stream_lines_compressed()
                    completed = True
                finally:
                    if completed:
                        temp_file.delete()
                        self.file_send_queue.task_done()
                    else:
                        with suppress(queue.Full):
                            self.file_send_queue.put_nowait(temp_file)

            return streamer
        except queue.Empty:
            return None

    def rotate_file(self) -> None:
        if self.file is not None:
            with self.lock:
                self.file.close()
                try:
                    self.file_send_queue.put_nowait(self.file)
                except queue.Full:
                    self.file.path.unlink()
                finally:
                    self.file = None

    def close(self) -> None:
        self.enabled = False
        self.rotate_file()
        while True:
            try:
                temp_file = self.file_send_queue.get_nowait()
                temp_file.delete()
                self.file_send_queue.task_done()
            except queue.Empty:
                break

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
