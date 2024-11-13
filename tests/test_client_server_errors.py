from pytest_mock import MockerFixture


def test_server_error_counter():
    from apitally.client.server_errors import ServerErrorCounter

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
    from apitally.client.server_errors import ServerErrorCounter

    mocker.patch("apitally.client.server_errors.MAX_EXCEPTION_MSG_LENGTH", 32)
    mocker.patch("apitally.client.server_errors.MAX_EXCEPTION_TRACEBACK_LENGTH", 128)

    try:
        raise ValueError("a" * 88)
    except ValueError as e:
        msg = ServerErrorCounter._get_truncated_exception_msg(e)
        tb = ServerErrorCounter._get_truncated_exception_traceback(e)

    assert len(msg) == 32
    assert msg.endswith("... (truncated)")
    assert len(tb) <= 128
    assert tb.startswith("... (truncated) ...\n")
