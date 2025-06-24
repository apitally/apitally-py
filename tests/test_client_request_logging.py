from __future__ import annotations

import base64
import gzip
import json
import time
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional
from urllib.parse import quote

import pytest


if TYPE_CHECKING:
    from apitally.client.request_logging import RequestDict, RequestLogger, ResponseDict


@pytest.fixture()
def request_logger() -> Iterator[RequestLogger]:
    from apitally.client.request_logging import RequestLogger, RequestLoggingConfig

    config = RequestLoggingConfig(
        enabled=True,
        log_query_params=True,
        log_request_headers=True,
        log_request_body=True,
        log_response_headers=True,
        log_response_body=True,
    )
    request_logger = RequestLogger(config=config)
    yield request_logger
    request_logger.close()


@pytest.fixture()
def request_dict() -> RequestDict:
    return {
        "timestamp": time.time(),
        "method": "GET",
        "path": "/test",
        "url": "http://localhost:8000/test?foo=bar",
        "headers": [("Accept", "text/plain"), ("Content-Type", "text/plain")],
        "size": 100,
        "consumer": "test",
        "body": b"test",
    }


@pytest.fixture()
def response_dict() -> ResponseDict:
    return {
        "status_code": 200,
        "response_time": 0.1,
        "headers": [("Content-Type", "text/plain")],
        "size": 100,
        "body": b"test",
    }


def get_logged_items(request_logger: RequestLogger) -> List[Dict[str, Any]]:
    request_logger.write_to_file()
    request_logger.rotate_file()
    file = request_logger.get_file()
    assert file is not None

    with file.open_compressed() as fp:
        data = gzip.decompress(fp.read())
    file.delete()

    lines = data.decode("utf-8").strip().split("\n")
    items = [json.loads(line) for line in lines]
    return items


async def test_request_logger_end_to_end(
    request_logger: RequestLogger, request_dict: RequestDict, response_dict: ResponseDict
):
    for _ in range(3):
        request_logger.log_request(request_dict, response_dict, RuntimeError("test"))

    request_logger.write_to_file()
    assert request_logger.current_file_size > 0

    request_logger.rotate_file()
    file = request_logger.get_file()
    assert file is not None

    compressed_data1 = b""
    async for chunk in file.stream_lines_compressed():
        compressed_data1 += chunk
    assert len(compressed_data1) > 0

    with file.open_compressed() as fp:
        compressed_data2 = fp.read()
    assert compressed_data1 == compressed_data2

    file.delete()

    decompressed_data = gzip.decompress(compressed_data1)
    json_lines = decompressed_data.decode("utf-8").strip().split("\n")
    assert len(json_lines) == 3

    for json_line in json_lines:
        item = json.loads(json_line)
        assert item["request"]["method"] == request_dict["method"]
        assert item["request"]["path"] == request_dict["path"]
        assert item["request"]["url"] == request_dict["url"]
        assert item["request"]["headers"] == [list(h) for h in request_dict["headers"]]
        assert item["request"]["size"] == request_dict["size"]
        assert item["request"]["consumer"] == request_dict["consumer"]
        assert item["response"]["status_code"] == response_dict["status_code"]
        assert item["response"]["response_time"] == response_dict["response_time"]
        assert item["response"]["headers"] == [list(h) for h in response_dict["headers"]]
        assert item["response"]["size"] == response_dict["size"]
        assert base64.b64decode(item["request"]["body"]) == request_dict["body"]
        assert base64.b64decode(item["response"]["body"]) == response_dict["body"]
        assert item["exception"]["type"] == "builtins.RuntimeError"
        assert item["exception"]["message"] == "test"


def test_request_log_config(request_logger: RequestLogger, request_dict: RequestDict, response_dict: ResponseDict):
    request_logger.config.log_query_params = False
    request_logger.config.log_request_headers = False
    request_logger.config.log_request_body = False
    request_logger.config.log_response_headers = False
    request_logger.config.log_response_body = False

    request_logger.log_request(request_dict, response_dict)
    items = get_logged_items(request_logger)
    assert len(items) == 1
    item = items[0]

    assert item["request"]["url"] == "http://localhost:8000/test"
    assert "headers" not in item["request"]
    assert "body" not in item["request"]
    assert "headers" not in item["response"]
    assert "body" not in item["response"]


def test_request_log_exclusion(request_logger: RequestLogger, request_dict: RequestDict, response_dict: ResponseDict):
    request_logger.config.exclude_paths = ["/excluded$"]
    request_logger.config.exclude_callback = lambda _, response: response["status_code"] == 404

    request_logger.log_request(request_dict, response_dict)
    assert len(request_logger.write_deque) == 1

    response_dict["status_code"] = 404
    request_logger.log_request(request_dict, response_dict)
    assert len(request_logger.write_deque) == 1

    response_dict["status_code"] = 200
    request_dict["path"] = "/api/excluded"
    request_logger.log_request(request_dict, response_dict)
    assert len(request_logger.write_deque) == 1

    request_dict["path"] = "/healthz"
    request_logger.log_request(request_dict, response_dict)
    assert len(request_logger.write_deque) == 1

    request_dict["path"] = "/"
    request_dict["headers"] = [("User-Agent", "ELB-HealthChecker/2.0")]
    request_logger.log_request(request_dict, response_dict)
    assert len(request_logger.write_deque) == 1


def test_request_log_mask_headers(
    request_logger: RequestLogger, request_dict: RequestDict, response_dict: ResponseDict
):
    from apitally.client.request_logging import MASKED

    request_logger.config.mask_headers = ["test"]
    request_logger.config.mask_query_params = ["test"]
    request_dict["headers"] += [("Authorization", "Bearer 123456"), ("X-Test", "123456")]

    request_logger.log_request(request_dict, response_dict)
    items = get_logged_items(request_logger)
    assert len(items) == 1
    item = items[0]

    assert ["Authorization", "Bearer 123456"] not in item["request"]["headers"]
    assert ["Authorization", MASKED] in item["request"]["headers"]
    assert ["X-Test", "123456"] not in item["request"]["headers"]
    assert ["X-Test", MASKED] in item["request"]["headers"]
    assert ["Accept", "text/plain"] in item["request"]["headers"]


def test_request_log_mask_query_params(
    request_logger: RequestLogger, request_dict: RequestDict, response_dict: ResponseDict
):
    from apitally.client.request_logging import MASKED

    request_logger.config.mask_query_params = ["test"]
    request_dict["url"] = "http://localhost/test?secret=123456&test=123456&other=abcdef"

    request_logger.log_request(request_dict, response_dict)
    items = get_logged_items(request_logger)
    assert len(items) == 1
    item = items[0]

    assert item["request"]["url"] == f"http://localhost/test?secret={quote(MASKED)}&test={quote(MASKED)}&other=abcdef"


def test_request_log_mask_body_callbacks(
    request_logger: RequestLogger, request_dict: RequestDict, response_dict: ResponseDict
):
    from apitally.client.request_logging import BODY_MASKED

    def mask_request_body_callback(request: RequestDict) -> Optional[bytes]:
        if request["method"] == "GET" and request["path"] == "/test":
            return None
        return request["body"]

    def mask_response_body_callback(request: RequestDict, response: ResponseDict) -> Optional[bytes]:
        if request["method"] == "GET" and request["path"] == "/test":
            return None
        return response["body"]

    request_logger.config.mask_request_body_callback = mask_request_body_callback
    request_logger.config.mask_response_body_callback = mask_response_body_callback

    request_logger.log_request(request_dict, response_dict)
    items = get_logged_items(request_logger)
    assert len(items) == 1
    item = items[0]

    assert base64.b64decode(item["request"]["body"]) == BODY_MASKED
    assert base64.b64decode(item["response"]["body"]) == BODY_MASKED


def test_request_log_mask_body_fields(
    request_logger: RequestLogger, request_dict: RequestDict, response_dict: ResponseDict
):
    from apitally.client.request_logging import MASKED

    request_logger.config.mask_body_fields = ["custom"]

    request_body = {
        "username": "john_doe",
        "password": "secret123",
        "token": "abc123",
        "custom": "xyz789",
        "user_id": 42,
        "api_key": 123,
        "normal_field": "value",
        "nested": {
            "password": "nested_secret",
            "count": 5,
            "deeper": {"auth": "deep_token"},
        },
        "array": [
            {"password": "array_secret", "id": 1},
            {"normal": "text", "token": "array_token"},
        ],
    }
    response_body = {
        "status": "success",
        "secret": "response_secret",
        "data": {"pwd": "response_pwd"},
    }

    request_dict["headers"] = [("Content-Type", "application/json")]
    request_dict["body"] = json.dumps(request_body).encode()
    response_dict["headers"] = [("Content-Type", "application/json")]
    response_dict["body"] = json.dumps(response_body).encode()

    request_logger.log_request(request_dict, response_dict)
    items = get_logged_items(request_logger)
    assert len(items) == 1
    item = items[0]

    masked_request_body = json.loads(base64.b64decode(item["request"]["body"]))
    masked_response_body = json.loads(base64.b64decode(item["response"]["body"]))

    # Test fields that should be masked
    assert masked_request_body["password"] == MASKED
    assert masked_request_body["token"] == MASKED
    assert masked_request_body["custom"] == MASKED
    assert masked_request_body["nested"]["password"] == MASKED
    assert masked_request_body["nested"]["deeper"]["auth"] == MASKED
    assert masked_request_body["array"][0]["password"] == MASKED
    assert masked_request_body["array"][1]["token"] == MASKED
    assert masked_response_body["secret"] == MASKED
    assert masked_response_body["data"]["pwd"] == MASKED

    # Test fields that should NOT be masked
    assert masked_request_body["username"] == "john_doe"
    assert masked_request_body["user_id"] == 42
    assert masked_request_body["api_key"] == 123
    assert masked_request_body["normal_field"] == "value"
    assert masked_request_body["nested"]["count"] == 5
    assert masked_request_body["array"][0]["id"] == 1
    assert masked_request_body["array"][1]["normal"] == "text"
    assert masked_response_body["status"] == "success"
