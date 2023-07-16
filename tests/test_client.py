from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, AsyncIterator
from unittest.mock import MagicMock

import pytest
from pytest_httpx import HTTPXMock
from pytest_mock import MockerFixture


if TYPE_CHECKING:
    from starlette_apitally.client import ApitallyClient


CLIENT_ID = "76b5cb91-a0a4-4ea0-a894-57d2b9fcb2c9"


@pytest.fixture()
async def client() -> AsyncIterator[ApitallyClient]:
    from starlette_apitally.client import ApitallyClient

    client = ApitallyClient(client_id=CLIENT_ID, env="default", send_every=0.01)
    try:
        await client.metrics.log_request(
            method="GET",
            path="/test",
            status_code=200,
            response_time=0.105,
        )
        await client.metrics.log_request(
            method="GET",
            path="/test",
            status_code=200,
            response_time=0.227,
        )
        yield client
    finally:
        client.stop_send_loop()
        await asyncio.sleep(0.02)


async def test_send_data(client: ApitallyClient, httpx_mock: HTTPXMock):
    from starlette_apitally.client import INGESTER_BASE_URL, INGESTER_VERSION

    httpx_mock.add_response()
    await asyncio.sleep(0.03)

    requests = httpx_mock.get_requests(url=f"{INGESTER_BASE_URL}/{INGESTER_VERSION}/{CLIENT_ID}/default/data")
    assert len(requests) == 1
    request_data = json.loads(requests[0].content)
    assert len(request_data["requests"]) == 1
    assert request_data["requests"][0]["request_count"] == 2


async def test_send_app_info(client: ApitallyClient, httpx_mock: HTTPXMock, mocker: MockerFixture):
    from starlette_apitally.client import INGESTER_BASE_URL, INGESTER_VERSION

    app_mock = MagicMock()
    httpx_mock.add_response()
    app_info = {"paths": [], "client_version": "1.0.0", "starlette_version": "0.28.0", "python_version": "3.11.4"}
    mocker.patch("starlette_apitally.client.get_app_info", return_value=app_info)
    client.send_app_info(app=app_mock, app_version="1.2.3", openapi_url="/openapi.json")
    await asyncio.sleep(0.01)

    requests = httpx_mock.get_requests(url=f"{INGESTER_BASE_URL}/{INGESTER_VERSION}/{CLIENT_ID}/default/info")
    assert len(requests) == 1
    request_data = json.loads(requests[0].content)
    assert request_data["paths"] == []
    assert request_data["client_version"] == "1.0.0"
