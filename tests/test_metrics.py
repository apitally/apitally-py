async def test_metrics():
    from starlette_apitally.metrics import RequestMetrics

    metrics = RequestMetrics()
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
    assert len(metrics.request_count) > 0

    data = await metrics.prepare_to_send()
    assert len(metrics.request_count) == 0
    assert len(data) == 1
    assert data[0]["method"] == "GET"
    assert data[0]["path"] == "/test"
    assert data[0]["status_code"] == 200
    assert data[0]["request_count"] == 2
    assert data[0]["response_times"] == [0.1, 0.2]
