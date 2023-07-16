from __future__ import annotations

import asyncio
import os
from asyncio import AbstractEventLoop
from importlib.util import find_spec
from typing import TYPE_CHECKING, Iterator
from unittest.mock import MagicMock

import pytest
from pytest import FixtureRequest
from pytest_mock import MockerFixture
from starlette.background import BackgroundTasks  # import here to avoid pydantic error


if TYPE_CHECKING:
    from starlette.applications import Starlette


if os.getenv("PYTEST_RAISE", "0") != "0":

    @pytest.hookimpl(tryfirst=True)
    def pytest_exception_interact(call):
        raise call.excinfo.value

    @pytest.hookimpl(tryfirst=True)
    def pytest_internalerror(excinfo):
        raise excinfo.value


@pytest.fixture(scope="session")
def client_id() -> str:
    return "76b5cb91-a0a4-4ea0-a894-57d2b9fcb2c9"


@pytest.fixture(scope="module")
def event_loop() -> Iterator[AbstractEventLoop]:
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(
    scope="module",
    params=["starlette", "fastapi"] if find_spec("fastapi") is not None else ["starlette"],
)
async def app(request: FixtureRequest, client_id: str, module_mocker: MockerFixture) -> Starlette:
    module_mocker.patch("starlette_apitally.client.ApitallyClient.start_send_loop")
    module_mocker.patch("starlette_apitally.client.ApitallyClient.send_app_info")
    if request.param == "starlette":
        return get_starlette_app(client_id)
    elif request.param == "fastapi":
        return get_fastapi_app(client_id)
    raise NotImplementedError


def get_starlette_app(client_id: str) -> Starlette:
    from starlette.applications import Starlette
    from starlette.background import BackgroundTask, BackgroundTasks
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

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
    app.add_middleware(ApitallyMiddleware, client_id=client_id)
    app.state.background_task_mock = background_task_mock
    return app


def get_fastapi_app(client_id: str) -> Starlette:
    from fastapi import FastAPI

    from starlette_apitally.middleware import ApitallyMiddleware

    background_task_mock = MagicMock()

    app = FastAPI(title="Test App", description="A simple test app.", version="1.2.3")
    app.add_middleware(ApitallyMiddleware, client_id=client_id)
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
