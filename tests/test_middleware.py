from __future__ import annotations

import pytest
from pytest_mock import MockerFixture
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


@pytest.fixture(scope="module")
def app():
    from starlette_apitally.middleware import ApitallyMiddleware

    def foo(request: Request):
        return PlainTextResponse("foo")

    def foo_bar(request: Request):
        return PlainTextResponse(f"foo: {request.path_params['bar']}")

    def bar(request: Request):
        raise ValueError("bar")

    routes = [
        Route("/foo/", foo),
        Route("/foo/{bar}/", foo_bar),
        Route("/bar/", bar, methods=["POST"]),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(ApitallyMiddleware, client_id="xxx")
    return app


def test_success(app: Starlette, mocker: MockerFixture):
    mock = mocker.patch("starlette_apitally.metrics.RequestMetrics.log_request")
    client = TestClient(app)

    response = client.get("/foo/")
    assert response.status_code == 200
    mock.assert_awaited_once()
    assert mock.await_args is not None
    assert mock.await_args.kwargs["method"] == "GET"
    assert mock.await_args.kwargs["path"] == "/foo/"
    assert mock.await_args.kwargs["status_code"] == 200
    assert mock.await_args.kwargs["response_time"] > 0

    response = client.get("/foo/123/")
    assert response.status_code == 200
    assert mock.await_count == 2
    assert mock.await_args is not None
    assert mock.await_args.kwargs["path"] == "/foo/{bar}/"


def test_error(app: Starlette, mocker: MockerFixture):
    mock = mocker.patch("starlette_apitally.metrics.RequestMetrics.log_request")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/bar/")
    assert response.status_code == 500
    mock.assert_awaited_once()
    assert mock.await_args is not None
    assert mock.await_args.kwargs["method"] == "POST"
    assert mock.await_args.kwargs["path"] == "/bar/"
    assert mock.await_args.kwargs["status_code"] == 500
    assert mock.await_args.kwargs["response_time"] > 0


def test_unhandled(app: Starlette, mocker: MockerFixture):
    mock = mocker.patch("starlette_apitally.metrics.RequestMetrics.log_request")
    client = TestClient(app)

    response = client.post("/baz/")
    assert response.status_code == 404
    mock.assert_not_awaited()
