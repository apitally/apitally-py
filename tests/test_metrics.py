from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, AsyncIterator

import pytest
from pytest_httpx import HTTPXMock


if TYPE_CHECKING:
    from starlette_apitally.metrics import Metrics


CLIENT_ID = "76b5cb91-a0a4-4ea0-a894-57d2b9fcb2c9"


@pytest.fixture()
async def metrics() -> AsyncIterator[Metrics]:
    from starlette_apitally.metrics import Metrics

    metrics = Metrics(client_id=CLIENT_ID, env="default", send_every=0.01)
    try:
        await metrics.log_request(
            method="GET",
            path="/test",
            status_code=200,
            response_time=0.105,
        )
        await metrics.log_request(
            method="GET",
            path="/test",
            status_code=200,
            response_time=0.227,
        )
        yield metrics
    finally:
        metrics.stop_send_loop()
        await asyncio.sleep(0.02)


async def test_get_and_reset_requests(metrics: Metrics):
    assert len(metrics.request_count) > 0

    data = await metrics.get_and_reset_requests()
    assert len(metrics.request_count) == 0
    assert len(data) == 1
    assert data[0]["method"] == "GET"
    assert data[0]["path"] == "/test"
    assert data[0]["status_code"] == 200
    assert data[0]["request_count"] == 2
    assert data[0]["response_times"][100] == 1
    assert data[0]["response_times"][220] == 1


async def test_send_data(metrics: Metrics, httpx_mock: HTTPXMock):
    from starlette_apitally.metrics import INGEST_BASE_URL

    httpx_mock.add_response()
    await asyncio.sleep(0.03)

    requests = httpx_mock.get_requests(url=f"{INGEST_BASE_URL}/v1/{CLIENT_ID}/default/data")
    assert len(requests) == 1
    request_data = json.loads(requests[0].content)
    assert len(request_data["requests"]) == 1
    assert request_data["requests"][0]["request_count"] == 2


async def test_send_app_info(metrics: Metrics, httpx_mock: HTTPXMock):
    from starlette_apitally.metrics import INGEST_BASE_URL

    httpx_mock.add_response()
    metrics.send_app_info(
        versions={
            "app_version": None,
            "client_version": "1.0.0",
            "starlette_version": "0.28.0",
            "python_version": "3.11.4",
        },
        openapi=None,
    )
    await asyncio.sleep(0.01)

    requests = httpx_mock.get_requests(url=f"{INGEST_BASE_URL}/v1/{CLIENT_ID}/default/info")
    assert len(requests) == 1
    request_data = json.loads(requests[0].content)
    assert request_data["versions"]["client_version"] == "1.0.0"
