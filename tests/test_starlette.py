from __future__ import annotations

from importlib.util import find_spec
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from pytest import FixtureRequest
from pytest_mock import MockerFixture


if find_spec("starlette") is None:
    pytest.skip("starlette is not available", allow_module_level=True)

if TYPE_CHECKING:
    from starlette.applications import Starlette

from starlette.background import BackgroundTasks  # import here to avoid pydantic error


CLIENT_ID = "76b5cb91-a0a4-4ea0-a894-57d2b9fcb2c9"
ENV = "default"


@pytest.fixture(
    scope="module",
    params=["starlette", "fastapi"] if find_spec("fastapi") is not None else ["starlette"],
)
async def app(request: FixtureRequest, module_mocker: MockerFixture) -> Starlette:
    module_mocker.patch("apitally.client.asyncio.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.asyncio.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.asyncio.ApitallyClient.send_app_info")
    if request.param == "starlette":
        return get_starlette_app()
    elif request.param == "fastapi":
        return get_fastapi_app()
    raise NotImplementedError


@pytest.fixture()
def app_with_auth() -> Starlette:
    from starlette.applications import Starlette
    from starlette.authentication import requires
    from starlette.middleware.authentication import AuthenticationMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route

    from apitally.starlette import ApitallyKeysBackend

    @requires(["authenticated", "foo"])
    def foo(request: Request):
        assert request.user.is_authenticated
        return PlainTextResponse("foo")

    @requires(["authenticated", "bar"])
    def bar(request: Request):
        return PlainTextResponse("bar")

    @requires("authenticated")
    def baz(request: Request):
        return JSONResponse(
            {
                "key_id": int(request.user.identity),
                "key_name": request.user.display_name,
                "key_scopes": request.auth.scopes,
            }
        )

    routes = [
        Route("/foo/", foo),
        Route("/bar/", bar),
        Route("/baz/", baz),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(AuthenticationMiddleware, backend=ApitallyKeysBackend())
    return app


def get_starlette_app() -> Starlette:
    from starlette.applications import Starlette
    from starlette.background import BackgroundTask, BackgroundTasks
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from apitally.starlette import ApitallyMiddleware

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
    app.add_middleware(ApitallyMiddleware, client_id=CLIENT_ID, env=ENV)
    app.state.background_task_mock = background_task_mock
    return app


def get_fastapi_app() -> Starlette:
    from fastapi import FastAPI

    from apitally.fastapi import ApitallyMiddleware

    background_task_mock = MagicMock()

    app = FastAPI(title="Test App", description="A simple test app.", version="1.2.3")
    app.add_middleware(ApitallyMiddleware, client_id=CLIENT_ID, env=ENV)
    app.state.background_task_mock = background_task_mock

    @app.get("/foo/")
    def foo(background_tasks: BackgroundTasks):
        background_tasks.add_task(background_task_mock)
        return "foo"

    @app.get("/foo/{bar}/")
    def foo_bar(bar: str, background_tasks: BackgroundTasks):
        background_tasks.add_task(background_task_mock)
        return f"foo: {bar}"

    @app.post("/bar/")
    def bar():
        return "bar"

    @app.post("/baz/")
    def baz():
        raise ValueError("baz")

    return app


def test_middleware_param_validation(app: Starlette):
    from apitally.starlette import ApitallyClient, ApitallyMiddleware

    ApitallyClient._instance = None

    with pytest.raises(ValueError):
        ApitallyMiddleware(app, client_id="76b5zb91-a0a4-4ea0-a894-57d2b9fcb2c9")
    with pytest.raises(ValueError):
        ApitallyMiddleware(app, client_id=CLIENT_ID, env="invalid.string")
    with pytest.raises(ValueError):
        ApitallyMiddleware(app, client_id=CLIENT_ID, app_version="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    with pytest.raises(ValueError):
        ApitallyMiddleware(app, client_id=CLIENT_ID, sync_interval=1)


def test_middleware_requests_ok(app: Starlette, mocker: MockerFixture):
    from starlette.testclient import TestClient

    mock = mocker.patch("apitally.client.base.RequestLogger.log_request")
    client = TestClient(app)
    background_task_mock: MagicMock = app.state.background_task_mock  # type: ignore[attr-defined]

    response = client.get("/foo/")
    assert response.status_code == 200
    background_task_mock.assert_called_once()
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "/foo/"
    assert mock.call_args.kwargs["status_code"] == 200
    assert mock.call_args.kwargs["response_time"] > 0

    response = client.get("/foo/123/")
    assert response.status_code == 200
    assert background_task_mock.call_count == 2
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

    mock = mocker.patch("apitally.client.base.RequestLogger.log_request")
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

    mock = mocker.patch("apitally.client.base.RequestLogger.log_request")
    client = TestClient(app)

    response = client.post("/xxx/")
    assert response.status_code == 404
    mock.assert_not_called()


def test_keys_auth_backend(app_with_auth: Starlette, mocker: MockerFixture):
    from starlette.testclient import TestClient

    from apitally.client.base import KeyInfo, KeyRegistry

    client = TestClient(app_with_auth)
    key_registry = KeyRegistry()
    key_registry.salt = "54fd2b80dbfeb87d924affbc91b77c76"
    key_registry.keys = {
        "bcf46e16814691991c8ed756a7ca3f9cef5644d4f55cd5aaaa5ab4ab4f809208": KeyInfo(
            key_id=1,
            name="Test key",
            scopes=["foo"],
        )
    }
    headers = {"Authorization": "ApiKey 7ll40FB.DuHxzQQuGQU4xgvYvTpmnii7K365j9VI"}
    mock = mocker.patch("apitally.starlette.ApitallyClient.get_instance")
    mock.return_value.key_registry = key_registry

    # Unauthenticated
    response = client.get("/foo")
    assert response.status_code == 403

    response = client.get("/baz")
    assert response.status_code == 403

    # Invalid API key
    response = client.get("/foo", headers={"Authorization": "ApiKey invalid"})
    assert response.status_code == 400

    # Valid API key with required scope
    response = client.get("/foo", headers=headers)
    assert response.status_code == 200

    # Valid API key, no scope required
    response = client.get("/baz", headers=headers)
    assert response.status_code == 200
    response_data = response.json()
    assert response_data["key_id"] == 1
    assert response_data["key_name"] == "Test key"
    assert response_data["key_scopes"] == ["authenticated", "foo"]

    # Valid API key without required scope
    response = client.get("/bar", headers=headers)
    assert response.status_code == 403


def test_get_app_info(app: Starlette, mocker: MockerFixture):
    from apitally.starlette import _get_app_info

    mocker.patch("apitally.starlette.ApitallyClient")
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()

    app_info = _get_app_info(app=app.middleware_stack, app_version=None, openapi_url="/openapi.json")
    assert ("paths" in app_info) != ("openapi" in app_info)  # only one, not both

    app_info = _get_app_info(app=app.middleware_stack, app_version="1.2.3", openapi_url=None)
    assert len(app_info["paths"]) == 4
    assert len(app_info["versions"]) > 1
    app_info["versions"]["app"] == "1.2.3"
