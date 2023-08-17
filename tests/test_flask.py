from __future__ import annotations

from importlib.util import find_spec
from typing import TYPE_CHECKING

import pytest
from pytest_mock import MockerFixture


if find_spec("flask") is None:
    pytest.skip("flask is not available", allow_module_level=True)

if TYPE_CHECKING:
    from flask import Flask


CLIENT_ID = "76b5cb91-a0a4-4ea0-a894-57d2b9fcb2c9"
ENV = "default"


@pytest.fixture(scope="module")
def app(module_mocker: MockerFixture) -> Flask:
    from flask import Flask

    from apitally.flask import ApitallyMiddleware

    module_mocker.patch("apitally.client.threading.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.threading.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.threading.ApitallyClient.send_app_info")
    module_mocker.patch("apitally.flask.ApitallyMiddleware.delayed_send_app_info")

    app = Flask("test")
    app.wsgi_app = ApitallyMiddleware(app.wsgi_app, client_id=CLIENT_ID, env=ENV)  # type: ignore[method-assign]

    @app.route("/foo/<bar>/")
    def foo_bar(bar: int):
        return f"foo: {bar}"

    @app.route("/bar/", methods=["POST"])
    def bar():
        return "bar"

    @app.route("/baz/", methods=["PUT"])
    def baz():
        raise ValueError("baz")

    return app


def test_middleware_requests_ok(app: Flask, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestLogger.log_request")
    client = app.test_client()

    response = client.get("/foo/123/")
    assert response.status_code == 200
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "/foo/<bar>/"
    assert mock.call_args.kwargs["status_code"] == 200
    assert mock.call_args.kwargs["response_time"] > 0

    response = client.post("/bar/")
    assert response.status_code == 200
    assert mock.call_count == 2
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "POST"


def test_middleware_requests_error(app: Flask, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestLogger.log_request")
    client = app.test_client()

    response = client.put("/baz/")
    assert response.status_code == 500
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "PUT"
    assert mock.call_args.kwargs["path"] == "/baz/"
    assert mock.call_args.kwargs["status_code"] == 500
    assert mock.call_args.kwargs["response_time"] > 0


def test_middleware_requests_unhandled(app: Flask, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestLogger.log_request")
    client = app.test_client()

    response = client.post("/xxx/")
    assert response.status_code == 404
    mock.assert_not_called()


def test_get_app_info(app: Flask):
    from apitally.flask import _get_app_info

    app_info = _get_app_info(app.wsgi_app, app.url_map, app_version="1.2.3", openapi_url="/openapi.json")
    assert len(app_info["paths"]) == 3
    assert len(app_info["versions"]) > 1
    app_info["versions"]["app"] == "1.2.3"
