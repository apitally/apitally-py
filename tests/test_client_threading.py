from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

import pytest
import requests
from pytest_mock import MockerFixture
from requests_mock import Mocker

from .constants import CLIENT_ID, ENV


if TYPE_CHECKING:
    from apitally.client.client_threading import ApitallyClient


@pytest.fixture(scope="module")
def client() -> ApitallyClient:
    from apitally.client.client_threading import ApitallyClient, RequestLoggingConfig

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


def test_sync_loop(client: ApitallyClient, mocker: MockerFixture):
    send_sync_data_mock = mocker.patch("apitally.client.client_threading.ApitallyClient.send_sync_data")
    mocker.patch("apitally.client.client_base.INITIAL_SYNC_INTERVAL", 0.05)

    client.start_sync_loop()
    time.sleep(0.02)  # Ensure loop enters first iteration
    client.stop_sync_loop()  # Should stop after first iteration
    assert client._thread is None
    assert send_sync_data_mock.call_count >= 1


def test_send_sync_data(client: ApitallyClient, requests_mock: Mocker):
    from apitally.client.client_base import HUB_BASE_URL, HUB_VERSION

    mock = requests_mock.register_uri("POST", f"{HUB_BASE_URL}/{HUB_VERSION}/{CLIENT_ID}/{ENV}/sync")
    with requests.Session() as session:
        client.send_sync_data(session)

    assert len(mock.request_history) == 1
    request_data = mock.request_history[0].json()
    assert len(request_data["requests"]) == 2
    assert request_data["requests"][0]["request_count"] == 2
    assert len(request_data["validation_errors"]) == 1
    assert request_data["validation_errors"][0]["error_count"] == 1


def test_send_log_data(client: ApitallyClient, requests_mock: Mocker):
    from apitally.client.client_base import HUB_BASE_URL, HUB_VERSION

    log_request(client)
    url_pattern = re.compile(rf"{HUB_BASE_URL}/{HUB_VERSION}/{CLIENT_ID}/{ENV}/log\?uuid=[a-f0-9-]+$")
    mock = requests_mock.register_uri("POST", url_pattern)
    with requests.Session() as session:
        client.send_log_data(session)

    assert len(mock.request_history) == 1
    # Ideally we'd also check the request body for correctness, but the following issue prevents us from doing so:
    # https://github.com/jamielennox/requests-mock/issues/243
    requests_mock.reset()

    # Test 402 response with Retry-After header
    log_request(client)
    mock = requests_mock.register_uri("POST", url_pattern, status_code=402, headers={"Retry-After": "3600"})
    with requests.Session() as session:
        client.send_log_data(session)

    assert len(mock.request_history) == 1
    assert client.request_logger.suspend_until is not None
    assert client.request_logger.suspend_until > time.time() + 3590

    # Ensure not logging requests anymore
    log_request(client)
    assert client.request_logger.file is None
    assert len(client.request_logger.write_deque) == 0
    assert len(client.request_logger.file_deque) == 0


def test_set_startup_data(client: ApitallyClient, requests_mock: Mocker):
    from apitally.client.client_base import HUB_BASE_URL, HUB_VERSION

    mock = requests_mock.register_uri("POST", f"{HUB_BASE_URL}/{HUB_VERSION}/{CLIENT_ID}/{ENV}/startup")
    data = {"paths": [], "client_version": "1.0.0", "starlette_version": "0.28.0", "python_version": "3.11.4"}
    client.set_startup_data(data)

    assert len(mock.request_history) == 1
    request_data = mock.request_history[0].json()
    assert request_data["paths"] == []
    assert request_data["client_version"] == "1.0.0"
