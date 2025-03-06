import base64
import gzip
import re
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from io import BufferedReader
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Mapping, Optional, Tuple, TypedDict
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import uuid4

from apitally.client.logging import get_logger


logger = get_logger(__name__)

MAX_BODY_SIZE = 50_000  # 50 KB (uncompressed)
MAX_FILE_SIZE = 1_000_000  # 1 MB (compressed)
MAX_REQUESTS_IN_DEQUE = 100  # Written to file every second, so limits logging to 100 rps
MAX_FILES_IN_DEQUE = 50
BODY_TOO_LARGE = b"<body too large>"
BODY_MASKED = b"<masked>"
MASKED = "******"
ALLOWED_CONTENT_TYPES = ["application/json", "text/plain"]
EXCLUDE_PATH_PATTERNS = [
    r"/_?healthz?$",
    r"/_?health[\-_]?checks?$",
    r"/_?heart[\-_]?beats?$",
    r"/ping$",
    r"/ready$",
    r"/live$",
]
EXCLUDE_USER_AGENT_PATTERNS = [
    r"health[\-_ ]?check",
    r"microsoft-azure-application-lb",
    r"googlehc",
    r"kube-probe",
]
MASK_QUERY_PARAM_PATTERNS = [
    r"auth",
    r"api-?key",
    r"secret",
    r"token",
    r"password",
    r"pwd",
]
MASK_HEADER_PATTERNS = [
    r"auth",
    r"api-?key",
    r"secret",
    r"token",
    r"cookie",
]


class RequestDict(TypedDict):
    timestamp: float
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


@dataclass
class RequestLoggingConfig:
    """
    Configuration for request logging.

    Attributes:
        enabled: Whether request logging is enabled
        log_query_params: Whether to log query parameter values
        log_request_headers: Whether to log request header values
        log_request_body: Whether to log the request body (only if JSON or plain text)
        log_response_headers: Whether to log response header values
        log_response_body: Whether to log the response body (only if JSON or plain text)
        mask_query_params: Query parameter names to mask in logs. Expects regular expressions.
        mask_headers: Header names to mask in logs. Expects regular expressions.
        mask_request_body_callback: Callback to mask the request body. Expects (method, path, body) and returns the masked body as bytes or None.
        mask_response_body_callback: Callback to mask the response body. Expects (method, path, body) and returns the masked body as bytes or None.
        exclude_paths: Paths to exclude from logging. Expects regular expressions.
        exclude_callback: Callback to exclude requests from logging. Should expect two arguments, `request: RequestDict` and `response: ResponseDict`, and return True to exclude the request.
    """

    enabled: bool = False
    log_query_params: bool = True
    log_request_headers: bool = False
    log_request_body: bool = False
    log_response_headers: bool = True
    log_response_body: bool = False
    mask_query_params: List[str] = field(default_factory=list)
    mask_headers: List[str] = field(default_factory=list)
    mask_request_body_callback: Optional[Callable[[RequestDict], Optional[bytes]]] = None
    mask_response_body_callback: Optional[Callable[[RequestDict, ResponseDict], Optional[bytes]]] = None
    exclude_paths: List[str] = field(default_factory=list)
    exclude_callback: Optional[Callable[[RequestDict, ResponseDict], bool]] = None


class TempGzipFile:
    def __init__(self) -> None:
        self.uuid = uuid4()
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

    async def stream_lines_compressed(self) -> AsyncIterator[bytes]:
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
        self.write_deque: deque[bytes] = deque([], MAX_REQUESTS_IN_DEQUE)
        self.file_deque: deque[TempGzipFile] = deque([])
        self.file: Optional[TempGzipFile] = None
        self.lock = threading.Lock()
        self.suspend_until: Optional[float] = None

    @property
    def current_file_size(self) -> int:
        return self.file.size if self.file is not None else 0

    def log_request(self, request: RequestDict, response: ResponseDict) -> None:
        if not self.enabled or self.suspend_until is not None:
            return
        parsed_url = urlparse(request["url"])
        user_agent = self._get_user_agent(request["headers"])
        if (
            self._should_exclude_path(request["path"] or parsed_url.path)
            or self._should_exclude_user_agent(user_agent)
            or self._should_exclude(request, response)
        ):
            return

        query = self._mask_query_params(parsed_url.query) if self.config.log_query_params else ""
        request["url"] = urlunparse(parsed_url._replace(query=query))

        if not self.config.log_request_body or not self._has_supported_content_type(request["headers"]):
            request["body"] = None
        elif (
            self.config.mask_request_body_callback is not None
            and request["body"] is not None
            and request["body"] != BODY_TOO_LARGE
        ):
            try:
                request["body"] = self.config.mask_request_body_callback(request)
            except Exception:  # pragma: no cover
                logger.exception("User-provided mask_request_body_callback function raised an exception")
                request["body"] = None
            if request["body"] is None:
                request["body"] = BODY_MASKED
        if request["body"] is not None and len(request["body"]) > MAX_BODY_SIZE:
            request["body"] = BODY_TOO_LARGE

        if not self.config.log_response_body or not self._has_supported_content_type(response["headers"]):
            response["body"] = None
        elif (
            self.config.mask_response_body_callback is not None
            and response["body"] is not None
            and response["body"] != BODY_TOO_LARGE
        ):
            try:
                response["body"] = self.config.mask_response_body_callback(request, response)
            except Exception:  # pragma: no cover
                logger.exception("User-provided mask_response_body_callback function raised an exception")
                response["body"] = None
            if response["body"] is None:
                response["body"] = BODY_MASKED
        if response["body"] is not None and len(response["body"]) > MAX_BODY_SIZE:
            response["body"] = BODY_TOO_LARGE

        request["headers"] = self._mask_headers(request["headers"]) if self.config.log_request_headers else []
        response["headers"] = self._mask_headers(response["headers"]) if self.config.log_response_headers else []

        item = {
            "uuid": str(uuid4()),
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

    def maintain(self) -> None:
        if self.current_file_size > MAX_FILE_SIZE:
            self.rotate_file()
        while len(self.file_deque) > MAX_FILES_IN_DEQUE:
            file = self.file_deque.popleft()
            file.delete()
        if self.suspend_until is not None and self.suspend_until < time.time():
            self.suspend_until = None

    def clear(self) -> None:
        self.write_deque.clear()
        self.rotate_file()
        for file in self.file_deque:
            file.delete()
        self.file_deque.clear()

    def close(self) -> None:
        self.enabled = False
        self.clear()

    def _should_exclude(self, request: RequestDict, response: ResponseDict) -> bool:
        if self.config.exclude_callback is not None:
            return self.config.exclude_callback(request, response)
        return False

    @lru_cache(maxsize=1000)
    def _should_exclude_path(self, url_path: str) -> bool:
        patterns = self.config.exclude_paths + EXCLUDE_PATH_PATTERNS
        return self._match_patterns(url_path, patterns)

    @lru_cache(maxsize=1000)
    def _should_exclude_user_agent(self, user_agent: Optional[str]) -> bool:
        return self._match_patterns(user_agent, EXCLUDE_USER_AGENT_PATTERNS) if user_agent is not None else False

    def _mask_query_params(self, query: str) -> str:
        query_params = parse_qsl(query)
        masked_query_params = [(k, v if not self._should_mask_query_param(k) else MASKED) for k, v in query_params]
        return urlencode(masked_query_params)

    def _mask_headers(self, headers: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
        return [(k, v if not self._should_mask_header(k) else MASKED) for k, v in headers]

    @lru_cache(maxsize=100)
    def _should_mask_query_param(self, query_param_name: str) -> bool:
        patterns = self.config.mask_query_params + MASK_QUERY_PARAM_PATTERNS
        return self._match_patterns(query_param_name, patterns)

    @lru_cache(maxsize=100)
    def _should_mask_header(self, header_name: str) -> bool:
        patterns = self.config.mask_headers + MASK_HEADER_PATTERNS
        return self._match_patterns(header_name, patterns)

    @staticmethod
    def _match_patterns(value: str, patterns: List[str]) -> bool:
        for pattern in patterns:
            if re.search(pattern, value, re.I) is not None:
                return True
        return False

    @staticmethod
    def _get_user_agent(headers: List[Tuple[str, str]]) -> Optional[str]:
        return next((v for k, v in headers if k.lower() == "user-agent"), None)

    @staticmethod
    def _has_supported_content_type(headers: List[Tuple[str, str]]) -> bool:
        content_type = next((v for k, v in headers if k.lower() == "content-type"), None)
        return RequestLogger.is_supported_content_type(content_type)

    @staticmethod
    def is_supported_content_type(content_type: Optional[str]) -> bool:
        return content_type is not None and any(content_type.startswith(t) for t in ALLOWED_CONTENT_TYPES)


def _check_writable_fs() -> bool:
    try:
        with tempfile.NamedTemporaryFile():
            return True
    except (IOError, OSError):  # pragma: no cover
        logger.error("Unable to create temporary file for request logging")
        return False


def _get_json_serializer() -> Callable[[Any], bytes]:
    def default(obj: Any) -> Any:
        if isinstance(obj, bytes):
            return base64.b64encode(obj).decode()
        raise TypeError  # pragma: no cover

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


def _skip_empty_values(data: Mapping) -> Dict:
    return {
        k: v for k, v in data.items() if v is not None and not (isinstance(v, (list, dict, bytes, str)) and len(v) == 0)
    }
