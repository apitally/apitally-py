from __future__ import annotations

import gzip
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from apitally.shared.config import MAX_BODY_SIZE


EVENT_NAME = "apitally.request.validation_error"
MAX_GROUPS = 100
MAX_SOURCE_LENGTH = 32
MAX_FIELD_LENGTH = 2_048
MAX_MESSAGE_LENGTH = 2_048
MAX_TYPE_LENGTH = 128
MAX_COUNT = 2**32 - 1

SOURCE_ALIASES = {
    "body": "body",
    "query": "query",
    "path": "path",
    "header": "header",
    "cookie": "cookie",
    "querystring": "query",
    "params": "path",
    "path_params": "path",
    "headers": "header",
    "cookies": "cookie",
}


@dataclass(frozen=True, slots=True)
class ValidationError:
    source: str
    field: str
    message: str
    type: str


@dataclass(frozen=True, slots=True)
class ValidationErrorKey:
    consumer: str | None
    method: str
    path: str
    source: str
    field: str
    message: str
    type: str


validation_error_lock = threading.Lock()
validation_error_counts: dict[ValidationErrorKey, int] = {}


def add_validation_errors(
    consumer: str | None,
    method: str,
    path: str,
    validation_errors: list[ValidationError],
) -> None:
    method = method.upper()
    with validation_error_lock:
        for error in validation_errors:
            key = ValidationErrorKey(
                consumer=consumer,
                method=method,
                path=path,
                source=error.source[:MAX_SOURCE_LENGTH],
                field=error.field[:MAX_FIELD_LENGTH],
                message=error.message[:MAX_MESSAGE_LENGTH],
                type=error.type[:MAX_TYPE_LENGTH],
            )
            if key in validation_error_counts:
                validation_error_counts[key] += 1
            elif len(validation_error_counts) < MAX_GROUPS:
                validation_error_counts[key] = 1


def record_validation_response(
    consumer: str | None,
    method: str,
    path: str | None,
    body: bytes,
    content_type: str | bytes | None,
    content_encoding: str | bytes | None,
    extractor: Callable[[object], list[ValidationError]],
) -> None:
    if method.upper() == "OPTIONS" or not path or not is_json_content_type(content_type):
        return
    data = decode_json_response(body, content_encoding)
    if data is not None:
        add_validation_errors(consumer, method, path, extractor(data))


def drain_validation_errors() -> list[dict[str, Any]]:
    global validation_error_counts
    with validation_error_lock:
        counts = validation_error_counts
        validation_error_counts = {}
    events = []
    for error, count in counts.items():
        body: dict[str, Any] = {
            "method": error.method,
            "path": error.path,
            "source": error.source,
            "field": error.field,
            "message": error.message,
            "type": error.type,
            "count": min(count, MAX_COUNT),
        }
        if error.consumer is not None:
            body["consumer"] = error.consumer
        events.append(body)
    return events


def reset() -> None:
    global validation_error_counts, validation_error_lock
    validation_error_counts = {}
    validation_error_lock = threading.Lock()


def format_location(location: object) -> tuple[str, str]:
    if not isinstance(location, list) or not location:
        return "", ""
    source = ""
    start = 0
    if isinstance(location[0], str):
        source = SOURCE_ALIASES.get(location[0].lower(), "")
        if source:
            start = 1
    field = ".".join(
        str(segment)
        for segment in location[start:]
        if isinstance(segment, (str, int)) and not isinstance(segment, bool)
    )
    return source, field


def extract_pydantic_validation_errors(data: object) -> list[ValidationError]:
    if not isinstance(data, Mapping):
        return []
    details = data.get("detail")
    if not isinstance(details, list):
        return []
    errors = []
    for detail in details:
        if not isinstance(detail, Mapping) or not all(key in detail for key in ("loc", "msg", "type")):
            continue
        source, field = format_location(detail.get("loc"))
        message = detail.get("msg")
        error_type = detail.get("type")
        errors.append(
            ValidationError(
                source=source,
                field=field,
                message=message if isinstance(message, str) else "",
                type=error_type if isinstance(error_type, str) else "",
            )
        )
    return errors


def is_json_content_type(content_type: str | bytes | None) -> bool:
    if isinstance(content_type, bytes):
        content_type = content_type.decode("latin1")
    if not content_type:
        return False
    media_type = content_type.partition(";")[0].strip().lower()
    return media_type == "application/json" or (media_type.startswith("application/") and media_type.endswith("+json"))


def decode_json_response(body: bytes, content_encoding: str | bytes | None) -> object | None:
    if len(body) > MAX_BODY_SIZE:
        return None
    if isinstance(content_encoding, bytes):
        content_encoding = content_encoding.decode("latin1")
    encoding = content_encoding.strip().lower() if content_encoding else ""
    try:
        if encoding == "gzip":
            with gzip.GzipFile(fileobj=BytesIO(body)) as gzip_file:
                body = gzip_file.read(MAX_BODY_SIZE + 1)
        elif encoding not in ("", "identity"):
            return None
        if len(body) > MAX_BODY_SIZE:
            return None
        return json.loads(body)
    except Exception:
        return None
