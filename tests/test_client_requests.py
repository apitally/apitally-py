def test_request_counter():
    from apitally.client.requests import RequestCounter

    requests = RequestCounter()
    requests.add_request(
        consumer=None,
        method="GET",
        path="/test",
        status_code=200,
        response_time=0.105,
        request_size=None,
        response_size="123",
    )
    requests.add_request(
        consumer=None,
        method="GET",
        path="/test",
        status_code=200,
        response_time=0.227,
        request_size=None,
        response_size="321",
    )
    requests.add_request(
        consumer=None,
        method="POST",
        path="/test",
        status_code=204,
        response_time=0.1,
        request_size="123",
        response_size=None,
    )
    assert len(requests.request_counts) == 2

    data = requests.get_and_reset_requests()
    assert len(requests.request_counts) == 0
    assert len(data) == 2
    assert data[0]["method"] == "GET"
    assert data[0]["path"] == "/test"
    assert data[0]["status_code"] == 200
    assert data[0]["request_count"] == 2
    assert data[0]["request_size_sum"] == 0
    assert data[0]["response_size_sum"] > 0
    assert data[0]["response_times"][100] == 1
    assert data[0]["response_times"][220] == 1
    assert len(data[0]["request_sizes"]) == 0
    assert data[0]["response_sizes"][0] == 2
    assert data[1]["method"] == "POST"
    assert data[1]["request_size_sum"] > 0
    assert data[1]["response_size_sum"] == 0
    assert data[1]["request_sizes"][0] == 1
