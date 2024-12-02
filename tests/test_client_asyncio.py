from __future__ import annotations

import asyncio
import gzip
import json
import re
import sys
import time
from typing import TYPE_CHECKING

import pytest
import pytest_httpx
from pytest_httpx import HTTPXMock
from pytest_mock import MockerFixture

from .constants import CLIENT_ID, ENV


if TYPE_CHECKING:
    from apitally.client.client_asyncio import ApitallyClient


@pytest.fixture(scope="module")
async def client() -> ApitallyClient:
    from apitally.client.client_asyncio import ApitallyClient, RequestLoggingConfig

    client = ApitallyClient(
        client_id=CLIENT_ID,
        env=ENV,
        request_logging_config=RequestLoggingConfig(enabled=True),
    )
    client.request_counter.add_request(
        consumer=None,
        method="GET",
        path="/test",
        status_code=200,
        response_time=0.105,
    )
    client.request_counter.add_request(
        consumer=None,
        method="GET",
        path="/test",
        status_code=200,
        response_time=0.227,
    )
    client.request_counter.add_request(
        consumer=None,
        method="GET",
        path="/test",
        status_code=422,
        response_time=0.02,
    )
    client.validation_error_counter.add_validation_errors(
        consumer=None,
        method="GET",
        path="/test",
        detail=[
            {
                "loc": ["query", "foo"],
                "type": "type_error.integer",
                "msg": "value is not a valid integer",
            },
        ],
    )
    return client


def log_request(client: ApitallyClient) -> None:
    client.request_logger.log_request(
        request={
            "timestamp": time.time(),
            "method": "GET",
            "path": "/test",
            "url": "http://testserver/test",
            "headers": [],
            "size": 0,
            "consumer": None,
            "body": None,
        },
        response={
            "status_code": 200,
            "response_time": 0.105,
            "headers": [],
            "size": 0,
            "body": None,
        },
    )
    client.request_logger.write_to_file()


async def test_sync_loop(client: ApitallyClient, mocker: MockerFixture):
    send_sync_data_mock = mocker.patch("apitally.client.client_asyncio.ApitallyClient.send_sync_data")
    mocker.patch("apitally.client.client_base.INITIAL_SYNC_INTERVAL", 0.05)

    client.start_sync_loop()
    await asyncio.sleep(0.2)  # Ensure loop starts
    client.stop_sync_loop()  # Should stop after next iteration
    await asyncio.sleep(0.1)  # Wait for task to finish
    assert send_sync_data_mock.await_count >= 1


async def test_send_sync_data(client: ApitallyClient, httpx_mock: HTTPXMock):
    from apitally.client.client_base import HUB_BASE_URL, HUB_VERSION

    httpx_mock.add_response()
    async with client.get_http_client() as http_client:
        await client.send_sync_data(client=http_client)

    request = httpx_mock.get_request(url=f"{HUB_BASE_URL}/{HUB_VERSION}/{CLIENT_ID}/{ENV}/sync")
    assert request is not None
    request_data = json.loads(request.read())
    assert len(request_data["requests"]) == 2
    assert request_data["requests"][0]["request_count"] == 2
    assert len(request_data["validation_errors"]) == 1
    assert request_data["validation_errors"][0]["error_count"] == 1


async def test_send_log_data(client: ApitallyClient, httpx_mock: HTTPXMock):
    from apitally.client.client_base import HUB_BASE_URL, HUB_VERSION

    log_request(client)
    httpx_mock.add_response()
    async with client.get_http_client() as http_client:
        await client.send_log_data(client=http_client)

    url_pattern = re.compile(rf"{HUB_BASE_URL}/{HUB_VERSION}/{CLIENT_ID}/{ENV}/log\?uuid=[a-f0-9-]+$")
    request = httpx_mock.get_request(url=url_pattern)
    assert request is not None

    if sys.version_info >= (3, 9):
        # This doesn't work in Python 3.8 because of a bug in httpx
        json_lines = gzip.decompress(request.read()).strip().split(b"\n")
        assert len(json_lines) == 1
        json_data = json.loads(json_lines[0])
        assert json_data["request"]["path"] == "/test"
        assert json_data["response"]["status_code"] == 200

    if pytest_httpx.__version__ < "0.31.0":
        httpx_mock.reset(True)  # type: ignore[call-arg]
    else:
        httpx_mock.reset()

    # Test 402 response with Retry-After header
    log_request(client)
    httpx_mock.add_response(status_code=402, headers={"Retry-After": "3600"})
    async with client.get_http_client() as http_client:
        await client.send_log_data(client=http_client)
    assert httpx_mock.get_request(url=url_pattern) is not None
    assert client.request_logger.suspend_until is not None
    assert client.request_logger.suspend_until > time.time() + 3590

    # Ensure not logging requests anymore
    log_request(client)
    assert client.request_logger.file is None
    assert len(client.request_logger.write_deque) == 0
    assert len(client.request_logger.file_deque) == 0


async def test_set_startup_data(client: ApitallyClient, httpx_mock: HTTPXMock):
    from apitally.client.client_base import HUB_BASE_URL, HUB_VERSION

    httpx_mock.add_response()
    data = {"paths": [], "client_version": "1.0.0", "starlette_version": "0.28.0", "python_version": "3.11.4"}
    client.set_startup_data(data)
    await asyncio.sleep(0.01)

    request = httpx_mock.get_request(url=f"{HUB_BASE_URL}/{HUB_VERSION}/{CLIENT_ID}/{ENV}/startup")
    assert request is not None
    request_data = json.loads(request.read())
    assert request_data["paths"] == []
    assert request_data["client_version"] == "1.0.0"
