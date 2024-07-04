from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
import requests
from pytest_mock import MockerFixture
from requests_mock import Mocker

from .constants import CLIENT_ID, ENV


if TYPE_CHECKING:
    from apitally.client.threading import ApitallyClient


@pytest.fixture(scope="module")
def client() -> ApitallyClient:
    from apitally.client.threading import ApitallyClient

    client = ApitallyClient(client_id=CLIENT_ID, env=ENV)
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


def test_sync_loop(client: ApitallyClient, mocker: MockerFixture):
    send_sync_data_mock = mocker.patch("apitally.client.threading.ApitallyClient.send_sync_data")
    mocker.patch("apitally.client.base.INITIAL_SYNC_INTERVAL", 0.05)

    client.start_sync_loop()
    time.sleep(0.02)  # Ensure loop enters first iteration
    client.stop_sync_loop()  # Should stop after first iteration
    assert client._thread is None
    assert send_sync_data_mock.call_count >= 1


def test_send_sync_data(client: ApitallyClient, requests_mock: Mocker):
    from apitally.client.base import HUB_BASE_URL, HUB_VERSION

    mock = requests_mock.register_uri("POST", f"{HUB_BASE_URL}/{HUB_VERSION}/{CLIENT_ID}/{ENV}/sync")
    with requests.Session() as session:
        client.send_sync_data(session)

    assert len(mock.request_history) == 1
    request_data = mock.request_history[0].json()
    assert len(request_data["requests"]) == 2
    assert request_data["requests"][0]["request_count"] == 2
    assert len(request_data["validation_errors"]) == 1
    assert request_data["validation_errors"][0]["error_count"] == 1


def test_set_startup_data(client: ApitallyClient, requests_mock: Mocker):
    from apitally.client.base import HUB_BASE_URL, HUB_VERSION

    mock = requests_mock.register_uri("POST", f"{HUB_BASE_URL}/{HUB_VERSION}/{CLIENT_ID}/{ENV}/startup")
    data = {"paths": [], "client_version": "1.0.0", "starlette_version": "0.28.0", "python_version": "3.11.4"}
    client.set_startup_data(data)

    assert len(mock.request_history) == 1
    request_data = mock.request_history[0].json()
    assert request_data["paths"] == []
    assert request_data["client_version"] == "1.0.0"
