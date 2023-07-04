from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture
from starlette.applications import Starlette
from starlette.background import BackgroundTask, BackgroundTasks
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


@pytest.fixture(scope="module")
def app():
    from starlette_apitally.middleware import ApitallyMiddleware

    background_task_mock = MagicMock()

    def foo(request: Request):
        return PlainTextResponse("foo", background=BackgroundTasks([BackgroundTask(background_task_mock)]))

    def foo_bar(request: Request):
        return PlainTextResponse(f"foo: {request.path_params['bar']}", background=BackgroundTask(background_task_mock))

    def bar(request: Request):
        return PlainTextResponse("bar")

    def baz(request: Request):
        raise ValueError("baz")

    routes = [
        Route("/foo/", foo),
        Route("/foo/{bar}/", foo_bar),
        Route("/bar/", bar, methods=["POST"]),
        Route("/baz/", baz, methods=["POST"]),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(ApitallyMiddleware, client_id="xxx")
    app.state.background_task_mock = background_task_mock
    return app


def test_success(app: Starlette, mocker: MockerFixture):
    mock = mocker.patch("starlette_apitally.metrics.Metrics.log_request")
    client = TestClient(app)
    background_task_mock: MagicMock = app.state.background_task_mock

    response = client.get("/foo/")
    assert response.status_code == 200
    background_task_mock.assert_called_once()
    mock.assert_awaited_once()
    assert mock.await_args is not None
    assert mock.await_args.kwargs["method"] == "GET"
    assert mock.await_args.kwargs["path"] == "/foo/"
    assert mock.await_args.kwargs["status_code"] == 200
    assert mock.await_args.kwargs["response_time"] > 0

    response = client.get("/foo/123/")
    assert response.status_code == 200
    assert background_task_mock.call_count == 2
    assert mock.await_count == 2
    assert mock.await_args is not None
    assert mock.await_args.kwargs["path"] == "/foo/{bar}/"

    response = client.post("/bar/")
    assert response.status_code == 200
    assert mock.await_count == 3
    assert mock.await_args is not None
    assert mock.await_args.kwargs["method"] == "POST"


def test_error(app: Starlette, mocker: MockerFixture):
    mock = mocker.patch("starlette_apitally.metrics.Metrics.log_request")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/baz/")
    assert response.status_code == 500
    mock.assert_awaited_once()
    assert mock.await_args is not None
    assert mock.await_args.kwargs["method"] == "POST"
    assert mock.await_args.kwargs["path"] == "/baz/"
    assert mock.await_args.kwargs["status_code"] == 500
    assert mock.await_args.kwargs["response_time"] > 0


def test_unhandled(app: Starlette, mocker: MockerFixture):
    mock = mocker.patch("starlette_apitally.metrics.Metrics.log_request")
    client = TestClient(app)

    response = client.post("/xxx/")
    assert response.status_code == 404
    mock.assert_not_awaited()
