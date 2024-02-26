from __future__ import annotations

from importlib.util import find_spec
from typing import TYPE_CHECKING

import pytest
from pytest import FixtureRequest
from pytest_mock import MockerFixture

from .constants import CLIENT_ID, ENV


if find_spec("starlette") is None:
    pytest.skip("starlette is not available", allow_module_level=True)

if TYPE_CHECKING:
    from starlette.applications import Starlette


@pytest.fixture(
    scope="module",
    params=["starlette", "fastapi"] if find_spec("fastapi") is not None else ["starlette"],
)
async def app(request: FixtureRequest, module_mocker: MockerFixture) -> Starlette:
    module_mocker.patch("apitally.client.asyncio.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.asyncio.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.asyncio.ApitallyClient.set_app_info")
    module_mocker.patch("apitally.starlette.ApitallyMiddleware.delayed_set_app_info")
    if request.param == "starlette":
        return get_starlette_app()
    elif request.param == "fastapi":
        return get_fastapi_app()
    raise NotImplementedError


def get_starlette_app() -> Starlette:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from apitally.starlette import ApitallyMiddleware

    def foo(request: Request):
        return PlainTextResponse("foo")

    def foo_bar(request: Request):
        return PlainTextResponse(f"foo: {request.path_params['bar']}")

    def bar(request: Request):
        return PlainTextResponse("bar")

    def baz(request: Request):
        raise ValueError("baz")

    def val(request: Request):
        return PlainTextResponse("validation error", status_code=422)

    routes = [
        Route("/foo/", foo),
        Route("/foo/{bar}/", foo_bar),
        Route("/bar/", bar, methods=["POST"]),
        Route("/baz/", baz, methods=["POST"]),
        Route("/val/", val),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(ApitallyMiddleware, client_id=CLIENT_ID, env=ENV)
    return app


def get_fastapi_app() -> Starlette:
    from fastapi import FastAPI, Query

    from apitally.fastapi import ApitallyMiddleware

    app = FastAPI(title="Test App", description="A simple test app.", version="1.2.3")
    app.add_middleware(ApitallyMiddleware, client_id=CLIENT_ID, env=ENV)

    @app.get("/foo/")
    def foo():
        return "foo"

    @app.get("/foo/{bar}/")
    def foo_bar(bar: str):
        return f"foo: {bar}"

    @app.post("/bar/")
    def bar():
        return "bar"

    @app.post("/baz/")
    def baz():
        raise ValueError("baz")

    @app.get("/val/")
    def val(foo: int = Query()):
        return "val"

    return app


def test_middleware_requests_ok(app: Starlette, mocker: MockerFixture):
    from starlette.testclient import TestClient

    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")
    client = TestClient(app)

    response = client.get("/foo/")
    assert response.status_code == 200
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "/foo/"
    assert mock.call_args.kwargs["status_code"] == 200
    assert mock.call_args.kwargs["response_time"] > 0

    response = client.get("/foo/123/")
    assert response.status_code == 200
    assert mock.call_count == 2
    assert mock.call_args is not None
    assert mock.call_args.kwargs["path"] == "/foo/{bar}/"

    response = client.post("/bar/")
    assert response.status_code == 200
    assert mock.call_count == 3
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "POST"


def test_middleware_requests_error(app: Starlette, mocker: MockerFixture):
    from starlette.testclient import TestClient

    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/baz/")
    assert response.status_code == 500
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "POST"
    assert mock.call_args.kwargs["path"] == "/baz/"
    assert mock.call_args.kwargs["status_code"] == 500
    assert mock.call_args.kwargs["response_time"] > 0


def test_middleware_requests_unhandled(app: Starlette, mocker: MockerFixture):
    from starlette.testclient import TestClient

    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")
    client = TestClient(app)

    response = client.post("/xxx/")
    assert response.status_code == 404
    mock.assert_not_called()


def test_middleware_validation_error(app: Starlette, mocker: MockerFixture):
    from starlette.testclient import TestClient

    mock = mocker.patch("apitally.client.base.ValidationErrorCounter.add_validation_errors")
    client = TestClient(app)

    # Validation error as foo must be an integer
    response = client.get("/val?foo=bar")
    assert response.status_code == 422

    # FastAPI only
    if response.headers["Content-Type"] == "application/json":
        mock.assert_called_once()
        assert mock.call_args is not None
        assert mock.call_args.kwargs["method"] == "GET"
        assert mock.call_args.kwargs["path"] == "/val/"
        assert len(mock.call_args.kwargs["detail"]) == 1
        assert mock.call_args.kwargs["detail"][0]["loc"] == ["query", "foo"]


def test_get_app_info(app: Starlette, mocker: MockerFixture):
    from apitally.starlette import _get_app_info

    mocker.patch("apitally.starlette.ApitallyClient")
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()

    app_info = _get_app_info(app=app.middleware_stack, app_version="1.2.3", openapi_url=None)
    assert len(app_info["paths"]) == 5
    assert app_info["versions"]["starlette"]
    assert app_info["versions"]["app"] == "1.2.3"
    assert app_info["client"] == "python:starlette"
