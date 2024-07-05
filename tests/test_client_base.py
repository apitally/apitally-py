from pytest_mock import MockerFixture


def test_request_counter():
    from apitally.client.base import RequestCounter

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


def test_validation_error_counter():
    from apitally.client.base import ValidationErrorCounter

    validation_errors = ValidationErrorCounter()
    validation_errors.add_validation_errors(
        consumer=None,
        method="GET",
        path="/test",
        detail=[
            {
                "loc": ["query", "foo"],
                "type": "type_error.integer",
                "msg": "value is not a valid integer",
            },
            {
                "loc": ["query", "bar"],
                "type": "type_error.integer",
                "msg": "value is not a valid integer",
            },
        ],
    )
    validation_errors.add_validation_errors(
        consumer=None,
        method="GET",
        path="/test",
        detail=[
            {
                "loc": ["query", "foo"],
                "type": "type_error.integer",
                "msg": "value is not a valid integer",
            }
        ],
    )

    data = validation_errors.get_and_reset_validation_errors()
    assert len(validation_errors.error_counts) == 0
    assert len(data) == 2
    assert data[0]["method"] == "GET"
    assert data[0]["path"] == "/test"
    assert data[0]["loc"] == ("query", "foo")
    assert data[0]["type"] == "type_error.integer"
    assert data[0]["msg"] == "value is not a valid integer"
    assert data[0]["error_count"] == 2


def test_server_error_counter():
    from apitally.client.base import ServerErrorCounter

    server_errors = ServerErrorCounter()
    server_errors.add_server_error(
        consumer=None,
        method="GET",
        path="/test",
        exception=ValueError("test"),
    )
    server_errors.add_server_error(
        consumer=None,
        method="GET",
        path="/test",
        exception=ValueError("test"),
    )

    data = server_errors.get_and_reset_server_errors()
    assert len(server_errors.error_counts) == 0
    assert len(data) == 1
    assert data[0]["method"] == "GET"
    assert data[0]["path"] == "/test"
    assert data[0]["type"] == "builtins.ValueError"
    assert data[0]["msg"] == "test"
    assert data[0]["error_count"] == 2


def test_exception_truncation(mocker: MockerFixture):
    from apitally.client.base import ServerErrorCounter

    mocker.patch("apitally.client.base.MAX_EXCEPTION_MSG_LENGTH", 32)
    mocker.patch("apitally.client.base.MAX_EXCEPTION_TRACEBACK_LENGTH", 128)

    try:
        raise ValueError("a" * 88)
    except ValueError as e:
        msg = ServerErrorCounter._get_truncated_exception_msg(e)
        tb = ServerErrorCounter._get_truncated_exception_traceback(e)

    assert len(msg) == 32
    assert msg.endswith("... (truncated)")
    assert len(tb) <= 128
    assert tb.startswith("... (truncated) ...\n")


def test_consumer_registry():
    from apitally.client.base import Consumer, ConsumerRegistry

    consumer_registry = ConsumerRegistry()
    consumer_registry.add_or_update_consumer(None)
    assert len(consumer_registry.consumers) == 0

    consumer_registry.add_or_update_consumer(Consumer("test"))
    assert len(consumer_registry.consumers) == 0

    consumer_registry.add_or_update_consumer(Consumer("test", name="Test"))
    consumer_registry.add_or_update_consumer(Consumer("test", group="Test"))
    assert len(consumer_registry.consumers) == 1
    assert consumer_registry.consumers["test"].name == "Test"
    assert consumer_registry.consumers["test"].group == "Test"

    consumer_registry.add_or_update_consumer(Consumer("test", group="Test 2"))
    assert len(consumer_registry.consumers) == 1
    assert consumer_registry.consumers["test"].name == "Test"
    assert consumer_registry.consumers["test"].group == "Test 2"

    consumer_registry.add_or_update_consumer(Consumer("test", name="Test 2"))
    assert len(consumer_registry.consumers) == 1
    assert consumer_registry.consumers["test"].name == "Test 2"
    assert consumer_registry.consumers["test"].group == "Test 2"

    assert len(consumer_registry.updated) == 1
    data = consumer_registry.get_and_reset_updated_consumers()
    assert len(data) == 1
    assert data[0]["identifier"] == "test"
    assert data[0]["name"] == "Test 2"
    assert data[0]["group"] == "Test 2"
    assert len(consumer_registry.updated) == 0
    assert len(consumer_registry.consumers) == 1
