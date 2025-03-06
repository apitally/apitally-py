from __future__ import annotations

from importlib.util import find_spec
from typing import TYPE_CHECKING

import pytest
from pytest_mock import MockerFixture

from .constants import CLIENT_ID, ENV


if find_spec("flask") is None:
    pytest.skip("flask is not available", allow_module_level=True)

if TYPE_CHECKING:
    from flask import Flask


@pytest.fixture(scope="module")
def app(module_mocker: MockerFixture) -> Flask:
    from flask import Flask, g, request

    from apitally.flask import ApitallyMiddleware, RequestLoggingConfig

    module_mocker.patch("apitally.client.client_threading.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.client_threading.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.client_threading.ApitallyClient.set_startup_data")
    module_mocker.patch("apitally.flask.ApitallyMiddleware.delayed_set_startup_data")

    app = Flask("test")
    app.wsgi_app = ApitallyMiddleware(  # type: ignore[method-assign]
        app,
        client_id=CLIENT_ID,
        env=ENV,
        request_logging_config=RequestLoggingConfig(
            enabled=True,
            log_request_body=True,
            log_response_body=True,
        ),
    )

    @app.route("/foo/<bar>")
    def foo_bar(bar: int):
        g.apitally_consumer = "test"
        return f"foo: {bar}", {"Content-Type": "text/plain"}

    @app.route("/bar", methods=["POST"])
    def bar():
        body = request.get_data()
        return "bar: " + body.decode(), {"Content-Type": "text/plain"}

    @app.route("/baz", methods=["PUT"])
    def baz():
        raise ValueError("baz")

    return app


def test_middleware_requests_ok(app: Flask, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.requests.RequestCounter.add_request")
    client = app.test_client()

    response = client.get("/foo/123")
    assert response.status_code == 200
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["consumer"] == "test"
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "/foo/<bar>"
    assert mock.call_args.kwargs["status_code"] == 200
    assert mock.call_args.kwargs["response_time"] > 0
    assert mock.call_args.kwargs["response_size"] > 0

    response = client.post("/bar")
    assert response.status_code == 200
    assert mock.call_count == 2
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "POST"


def test_middleware_requests_error(app: Flask, mocker: MockerFixture):
    mock1 = mocker.patch("apitally.client.requests.RequestCounter.add_request")
    mock2 = mocker.patch("apitally.client.server_errors.ServerErrorCounter.add_server_error")
    client = app.test_client()

    response = client.put("/baz")
    assert response.status_code == 500
    mock1.assert_called_once()
    assert mock1.call_args is not None
    assert mock1.call_args.kwargs["method"] == "PUT"
    assert mock1.call_args.kwargs["path"] == "/baz"
    assert mock1.call_args.kwargs["status_code"] == 500
    assert mock1.call_args.kwargs["response_time"] > 0

    mock2.assert_called_once()
    assert mock2.call_args is not None
    exception = mock2.call_args.kwargs["exception"]
    assert isinstance(exception, ValueError)


def test_middleware_requests_unhandled(app: Flask, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.requests.RequestCounter.add_request")
    client = app.test_client()

    response = client.post("/xxx")
    assert response.status_code == 404
    mock.assert_not_called()


def test_middleware_request_logging(app: Flask, mocker: MockerFixture):
    from apitally.client.request_logging import BODY_TOO_LARGE

    mock = mocker.patch("apitally.client.request_logging.RequestLogger.log_request")
    client = app.test_client()

    response = client.get("/foo/123?foo=bar", headers={"Test-Header": "test"})
    assert response.status_code == 200
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["request"]["method"] == "GET"
    assert mock.call_args.kwargs["request"]["path"] == "/foo/<bar>"
    assert mock.call_args.kwargs["request"]["url"] == "http://localhost/foo/123?foo=bar"
    assert ("Test-Header", "test") in mock.call_args.kwargs["request"]["headers"]
    assert mock.call_args.kwargs["response"]["status_code"] == 200
    assert mock.call_args.kwargs["response"]["response_time"] > 0
    assert ("Content-Type", "text/plain") in mock.call_args.kwargs["response"]["headers"]
    assert mock.call_args.kwargs["response"]["size"] > 0

    response = client.post("/bar", data=b"foo")
    assert response.status_code == 200
    assert mock.call_count == 2
    assert mock.call_args is not None
    assert mock.call_args.kwargs["request"]["method"] == "POST"
    assert mock.call_args.kwargs["request"]["path"] == "/bar"
    assert mock.call_args.kwargs["request"]["url"] == "http://localhost/bar"
    assert mock.call_args.kwargs["request"]["body"] == b"foo"
    assert mock.call_args.kwargs["response"]["body"] == b"bar: foo"

    mocker.patch("apitally.flask.MAX_BODY_SIZE", 2)
    response = client.post("/bar", data=b"foo")
    assert response.status_code == 200
    assert mock.call_count == 3
    assert mock.call_args is not None
    assert mock.call_args.kwargs["request"]["body"] == BODY_TOO_LARGE
    assert mock.call_args.kwargs["response"]["body"] == BODY_TOO_LARGE


def test_get_startup_data(app: Flask):
    from apitally.flask import _get_startup_data

    data = _get_startup_data(app, app_version="1.2.3", openapi_url="/openapi.json")
    assert len(data["paths"]) == 3
    assert data["versions"]["flask"]
    assert data["versions"]["app"] == "1.2.3"
    assert data["client"] == "python:flask"
