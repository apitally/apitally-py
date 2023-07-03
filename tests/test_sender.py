from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, AsyncIterator

import pytest
from pytest_httpx import HTTPXMock


if TYPE_CHECKING:
    from starlette_apitally.sender import Sender


@pytest.fixture()
async def sender() -> AsyncIterator[Sender]:
    from starlette_apitally.metrics import RequestMetrics
    from starlette_apitally.sender import Sender

    metrics = RequestMetrics()
    sender = Sender(metrics=metrics, client_id="xxx", send_every=0.01)
    try:
        await metrics.log_request(
            method="GET",
            path="/test",
            status_code=200,
            response_time=0.1,
        )
        yield sender
    finally:
        sender.stop_loop()
        await asyncio.sleep(0.02)


async def test_sender(sender: Sender, httpx_mock: HTTPXMock):
    from starlette_apitally.sender import BASE_URL

    httpx_mock.add_response(url=f"{BASE_URL}/xxx/")
    assert sender.base_url == f"{BASE_URL}/xxx"
    await asyncio.sleep(0.03)

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    request_data = json.loads(requests[0].content)
    assert len(request_data) == 1
    assert request_data[0]["request_count"] == 1
