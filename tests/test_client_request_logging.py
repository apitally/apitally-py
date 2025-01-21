from __future__ import annotations

import base64
import gzip
import json
import time
from typing import TYPE_CHECKING, Iterator, Optional
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


async def test_request_logger_end_to_end(
    request_logger: RequestLogger, request_dict: RequestDict, response_dict: ResponseDict
):
    for _ in range(3):
        request_logger.log_request(request_dict, response_dict)

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


def test_request_log_exclusion(request_logger: RequestLogger, request_dict: RequestDict, response_dict: ResponseDict):
    request_logger.config.log_query_params = False
    request_logger.config.log_request_headers = False
    request_logger.config.log_request_body = False
    request_logger.config.log_response_headers = False
    request_logger.config.log_response_body = False
    request_logger.config.exclude_paths = ["/excluded$"]
    request_logger.config.exclude_callback = lambda _, response: response["status_code"] == 404

    request_logger.log_request(request_dict, response_dict)
    assert len(request_logger.write_deque) == 1
    item = json.loads(request_logger.write_deque[0])
    assert item["request"]["url"] == "http://localhost:8000/test"
    assert "headers" not in item["request"]
    assert "body" not in item["request"]
    assert "headers" not in item["response"]
    assert "body" not in item["response"]

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


def test_request_log_masking(request_logger: RequestLogger, request_dict: RequestDict, response_dict: ResponseDict):
    from apitally.client.request_logging import BODY_MASKED, MASKED

    MASKED_QUOTED = quote(MASKED)

    def mask_request_body_callback(request: RequestDict) -> Optional[bytes]:
        if request["method"] == "GET" and request["path"] == "/test":
            return None
        return request["body"]

    def mask_response_body_callback(request: RequestDict, response: ResponseDict) -> Optional[bytes]:
        if request["method"] == "GET" and request["path"] == "/test":
            return None
        return response["body"]

    request_logger.config.mask_headers = ["test"]
    request_logger.config.mask_query_params = ["test"]
    request_logger.config.mask_request_body_callback = mask_request_body_callback
    request_logger.config.mask_response_body_callback = mask_response_body_callback
    request_dict["url"] = "http://localhost/test?secret=123456&test=123456&other=abcdef"
    request_dict["headers"] += [("Authorization", "Bearer 123456"), ("X-Test", "123456")]
    request_logger.log_request(request_dict, response_dict)

    item = json.loads(request_logger.write_deque[0])
    assert item["request"]["url"] == f"http://localhost/test?secret={MASKED_QUOTED}&test={MASKED_QUOTED}&other=abcdef"
    assert ["Authorization", "Bearer 123456"] not in item["request"]["headers"]
    assert ["Authorization", MASKED] in item["request"]["headers"]
    assert ["X-Test", "123456"] not in item["request"]["headers"]
    assert ["X-Test", MASKED] in item["request"]["headers"]
    assert ["Accept", "text/plain"] in item["request"]["headers"]
    assert item["request"]["body"] == base64.b64encode(BODY_MASKED).decode()
    assert item["response"]["body"] == base64.b64encode(BODY_MASKED).decode()
