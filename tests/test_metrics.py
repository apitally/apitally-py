from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, AsyncIterator

import pytest
from pytest_httpx import HTTPXMock


if TYPE_CHECKING:
    from starlette_apitally.metrics import Metrics


@pytest.fixture()
async def metrics() -> AsyncIterator[Metrics]:
    from starlette_apitally.metrics import Metrics

    metrics = Metrics(client_id="xxx", send_every=0.01)
    try:
        await metrics.log_request(
            method="GET",
            path="/test",
            status_code=200,
            response_time=0.1,
        )
        await metrics.log_request(
            method="GET",
            path="/test",
            status_code=200,
            response_time=0.2,
        )
        yield metrics
    finally:
        metrics.stop_send_loop()
        await asyncio.sleep(0.02)


async def test_prepare_to_send(metrics: Metrics):
    assert len(metrics.request_count) > 0

    data = await metrics.prepare_to_send()
    assert len(metrics.request_count) == 0
    assert len(data) == 1
    assert data[0]["method"] == "GET"
    assert data[0]["path"] == "/test"
    assert data[0]["status_code"] == 200
    assert data[0]["request_count"] == 2
    assert data[0]["response_times"] == [0.1, 0.2]


async def test_send(metrics: Metrics, httpx_mock: HTTPXMock):
    from starlette_apitally.metrics import BASE_URL

    httpx_mock.add_response(url=f"{BASE_URL}/xxx/")
    assert metrics.base_url == f"{BASE_URL}/xxx"
    await asyncio.sleep(0.03)

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    request_data = json.loads(requests[0].content)
    assert len(request_data) == 1
    assert request_data[0]["request_count"] == 2
