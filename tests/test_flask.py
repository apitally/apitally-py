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

    from apitally.client.base import KeyRegistry


@pytest.fixture(scope="module")
def app(module_mocker: MockerFixture) -> Flask:
    from flask import Flask

    from apitally.flask import ApitallyMiddleware

    module_mocker.patch("apitally.client.threading.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.threading.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.threading.ApitallyClient.set_app_info")
    module_mocker.patch("apitally.flask.ApitallyMiddleware.delayed_set_app_info")

    app = Flask("test")
    app.wsgi_app = ApitallyMiddleware(app, client_id=CLIENT_ID, env=ENV)  # type: ignore[method-assign]

    @app.route("/foo/<bar>")
    def foo_bar(bar: int):
        return f"foo: {bar}"

    @app.route("/bar", methods=["POST"])
    def bar():
        return "bar"

    @app.route("/baz", methods=["PUT"])
    def baz():
        raise ValueError("baz")

    return app


@pytest.fixture(scope="module")
def app_with_auth(module_mocker: MockerFixture) -> Flask:
    from flask import Flask, g, request

    from apitally.flask import ApitallyMiddleware, require_api_key

    module_mocker.patch("apitally.client.threading.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.threading.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.threading.ApitallyClient.set_app_info")
    module_mocker.patch("apitally.flask.ApitallyMiddleware.delayed_set_app_info")

    app = Flask("test")
    app.wsgi_app = ApitallyMiddleware(app, client_id=CLIENT_ID, env=ENV)  # type: ignore[method-assign]

    @app.before_request
    def identify_consumer():
        if consumer := request.args.get("consumer"):
            g.consumer_identifier = consumer

    @app.route("/foo")
    @require_api_key(scopes=["foo"])
    def foo():
        return "foo"

    @app.route("/bar")
    @require_api_key(custom_header="ApiKey", scopes=["bar"])
    def bar():
        return "bar"

    @app.route("/baz")
    @require_api_key
    def baz():
        g.consumer_identifier = "baz"
        return "baz"

    return app


def test_middleware_requests_ok(app: Flask, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")
    client = app.test_client()

    response = client.get("/foo/123")
    assert response.status_code == 200
    mock.assert_called_once()
    assert mock.call_args is not None
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
    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")
    client = app.test_client()

    response = client.put("/baz")
    assert response.status_code == 500
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "PUT"
    assert mock.call_args.kwargs["path"] == "/baz"
    assert mock.call_args.kwargs["status_code"] == 500
    assert mock.call_args.kwargs["response_time"] > 0


def test_middleware_requests_unhandled(app: Flask, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")
    client = app.test_client()

    response = client.post("/xxx")
    assert response.status_code == 404
    mock.assert_not_called()


def test_require_api_key(app_with_auth: Flask, key_registry: KeyRegistry, mocker: MockerFixture):
    client = app_with_auth.test_client()
    headers = {"Authorization": "ApiKey 7ll40FB.DuHxzQQuGQU4xgvYvTpmnii7K365j9VI"}
    headers_custom = {"ApiKey": "7ll40FB.DuHxzQQuGQU4xgvYvTpmnii7K365j9VI"}
    client_get_instance_mock = mocker.patch("apitally.flask.ApitallyClient.get_instance")
    client_get_instance_mock.return_value.key_registry = key_registry
    log_request_mock = mocker.patch("apitally.client.base.RequestCounter.add_request")

    # Unauthenticated
    response = client.get("/foo")
    assert response.status_code == 401

    response = client.get("/baz")
    assert response.status_code == 401

    # Invalid auth scheme
    response = client.get("/foo", headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 401

    # Invalid API key
    response = client.get("/foo", headers={"Authorization": "ApiKey invalid"})
    assert response.status_code == 403

    # Invalid API key, custom header
    response = client.get("/bar", headers={"ApiKey": "invalid"})
    assert response.status_code == 403

    # Valid API key with required scope
    response = client.get("/foo", headers=headers)
    assert response.status_code == 200
    assert log_request_mock.call_args.kwargs["consumer"] == "key:1"

    # Valid API key with required scope, identify consumer with custom function
    response = client.get("/foo?consumer=foo", headers=headers)
    assert response.status_code == 200
    assert log_request_mock.call_args.kwargs["consumer"] == "foo"

    # Valid API key, no scope required
    response = client.get("/baz", headers=headers)
    assert response.status_code == 200
    assert log_request_mock.call_args.kwargs["consumer"] == "baz"

    # Valid API key without required scope, custom header
    response = client.get("/bar", headers=headers_custom)
    assert response.status_code == 403


def test_get_app_info(app: Flask):
    from apitally.flask import _get_app_info

    app_info = _get_app_info(app, app_version="1.2.3", openapi_url="/openapi.json")
    assert len(app_info["paths"]) == 3
    assert app_info["versions"]["flask"]
    assert app_info["versions"]["app"] == "1.2.3"
    assert app_info["client"] == "python:flask"
