from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from pytest_httpx import HTTPXMock
from pytest_mock import MockerFixture

from .constants import CLIENT_ID, ENV


if TYPE_CHECKING:
    from apitally.client.asyncio import ApitallyClient


@pytest.fixture(scope="module")
async def client() -> ApitallyClient:
    from apitally.client.asyncio import ApitallyClient

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


async def test_sync_loop(client: ApitallyClient, mocker: MockerFixture):
    send_sync_data_mock = mocker.patch("apitally.client.asyncio.ApitallyClient.send_sync_data")
    mocker.patch("apitally.client.base.INITIAL_SYNC_INTERVAL", 0.05)

    client.start_sync_loop()
    await asyncio.sleep(0.2)  # Ensure loop starts
    client.stop_sync_loop()  # Should stop after next iteration
    await asyncio.sleep(0.1)  # Wait for task to finish
    assert send_sync_data_mock.await_count >= 1


async def test_send_sync_data(client: ApitallyClient, httpx_mock: HTTPXMock):
    from apitally.client.base import HUB_BASE_URL, HUB_VERSION

    httpx_mock.add_response()
    async with client.get_http_client() as http_client:
        await client.send_sync_data(client=http_client)

    requests = httpx_mock.get_requests(url=f"{HUB_BASE_URL}/{HUB_VERSION}/{CLIENT_ID}/{ENV}/sync")
    assert len(requests) == 1
    request_data = json.loads(requests[0].content)
    assert len(request_data["requests"]) == 2
    assert request_data["requests"][0]["request_count"] == 2
    assert len(request_data["validation_errors"]) == 1
    assert request_data["validation_errors"][0]["error_count"] == 1


async def test_set_startup_data(client: ApitallyClient, httpx_mock: HTTPXMock):
    from apitally.client.base import HUB_BASE_URL, HUB_VERSION

    httpx_mock.add_response()
    data = {"paths": [], "client_version": "1.0.0", "starlette_version": "0.28.0", "python_version": "3.11.4"}
    client.set_startup_data(data)
    await asyncio.sleep(0.01)

    requests = httpx_mock.get_requests(url=f"{HUB_BASE_URL}/{HUB_VERSION}/{CLIENT_ID}/{ENV}/startup")
    assert len(requests) == 1
    request_data = json.loads(requests[0].content)
    assert request_data["paths"] == []
    assert request_data["client_version"] == "1.0.0"
