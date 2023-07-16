from __future__ import annotations

from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from starlette_apitally.metrics import Metrics


@pytest.fixture()
async def metrics() -> Metrics:
    from starlette_apitally.metrics import Metrics

    metrics = Metrics()
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
    return metrics


async def test_metrics_get_and_reset_requests(metrics: Metrics):
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


def test_get_load_average(metrics: Metrics):
    load = metrics.get_load_average()
    assert load is not None
    assert load["1m"] > 0
    assert load["5m"] > 0
    assert load["15m"] > 0
