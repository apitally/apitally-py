from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, AsyncIterator

import pytest
from pytest_httpx import HTTPXMock
from pytest_mock import MockerFixture


if TYPE_CHECKING:
    from apitally.client import ApitallyClient


CLIENT_ID = "76b5cb91-a0a4-4ea0-a894-57d2b9fcb2c9"
ENV = "default"


@pytest.fixture()
async def client(mocker: MockerFixture) -> AsyncIterator[ApitallyClient]:
    from apitally.client import ApitallyClient

    mocker.patch("apitally.client.ApitallyClient.start_sync_loop")
    mocker.patch("apitally.client.ApitallyClient._run_sync_loop")

    client = ApitallyClient(client_id=CLIENT_ID, env=ENV, enable_keys=True)
    client.request_logger.log_request(
        method="GET",
        path="/test",
        status_code=200,
        response_time=0.105,
    )
    client.request_logger.log_request(
        method="GET",
        path="/test",
        status_code=200,
        response_time=0.227,
    )
    yield client


async def test_send_requests_data(client: ApitallyClient, httpx_mock: HTTPXMock):
    from apitally.client import HUB_BASE_URL, HUB_VERSION

    httpx_mock.add_response()
    async with client.get_http_client() as http_client:
        await client.send_requests_data(client=http_client)

    requests = httpx_mock.get_requests(url=f"{HUB_BASE_URL}/{HUB_VERSION}/{CLIENT_ID}/{ENV}/requests")
    assert len(requests) == 1
    request_data = json.loads(requests[0].content)
    assert len(request_data["requests"]) == 1
    assert request_data["requests"][0]["request_count"] == 2


async def test_send_app_info(client: ApitallyClient, httpx_mock: HTTPXMock):
    from apitally.client import HUB_BASE_URL, HUB_VERSION

    httpx_mock.add_response()
    app_info = {"paths": [], "client_version": "1.0.0", "starlette_version": "0.28.0", "python_version": "3.11.4"}
    client.send_app_info(app_info=app_info)
    await asyncio.sleep(0.01)

    requests = httpx_mock.get_requests(url=f"{HUB_BASE_URL}/{HUB_VERSION}/{CLIENT_ID}/{ENV}/info")
    assert len(requests) == 1
    request_data = json.loads(requests[0].content)
    assert request_data["paths"] == []
    assert request_data["client_version"] == "1.0.0"


async def test_get_keys(client: ApitallyClient, httpx_mock: HTTPXMock):
    from apitally.client import HUB_BASE_URL, HUB_VERSION

    httpx_mock.add_response(json={"salt": "x", "keys": {"x": {"key_id": 1, "expires_in_seconds": None}}})
    await client.get_keys()
    await asyncio.sleep(0.01)

    requests = httpx_mock.get_requests(url=f"{HUB_BASE_URL}/{HUB_VERSION}/{CLIENT_ID}/{ENV}/keys")
    assert len(requests) == 1
    assert len(client.key_registry.keys) == 1
