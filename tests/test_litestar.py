from __future__ import annotations

from importlib.util import find_spec
from typing import TYPE_CHECKING, Optional

import pytest
from pytest_mock import MockerFixture

from .constants import CLIENT_ID, ENV


if find_spec("litestar") is None:
    pytest.skip("litestar is not available", allow_module_level=True)
else:
    # Need to import Stream at package level to avoid NameError in Litestar
    from litestar.response import Stream


if TYPE_CHECKING:
    from litestar.app import Litestar
    from litestar.testing import TestClient


@pytest.fixture(scope="module")
async def app(module_mocker: MockerFixture) -> Litestar:
    from litestar.app import Litestar
    from litestar.connection import Request
    from litestar.handlers import get, post
    from litestar.response import Stream

    from apitally.litestar import ApitallyConsumer, ApitallyPlugin

    module_mocker.patch("apitally.client.asyncio.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.asyncio.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.asyncio.ApitallyClient.set_startup_data")

    @get("/foo")
    async def foo() -> str:
        return "foo"

    @get("/foo/{bar:str}")
    async def foo_bar(request: Request, bar: str) -> str:
        request.state.apitally_consumer = "test2"
        return f"foo: {bar}"

    @post("/bar")
    async def bar() -> str:
        return "bar"

    @post("/baz")
    async def baz() -> None:
        raise ValueError("baz")

    @get("/val")
    async def val(foo: int) -> str:
        return "val"

    @get("/stream")
    async def stream() -> Stream:
        def stream_response():
            yield b"foo"
            yield b"bar"

        return Stream(stream_response())

    def identify_consumer(request: Request) -> Optional[ApitallyConsumer]:
        return ApitallyConsumer("test1", name="Test 1") if "/foo" in request.route_handler.paths else None

    plugin = ApitallyPlugin(
        client_id=CLIENT_ID,
        env=ENV,
        app_version="1.2.3",
        identify_consumer_callback=identify_consumer,
    )
    app = Litestar(
        route_handlers=[foo, foo_bar, bar, baz, val, stream],
        plugins=[plugin],
    )
    return app


@pytest.fixture(scope="module")
async def client(app: Litestar) -> TestClient:
    from litestar.testing import TestClient

    with TestClient(app) as client:
        return client


def test_middleware_requests_ok(client: TestClient, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")

    response = client.get("/foo/")
    assert response.status_code == 200
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["consumer"] == "test1"
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "/foo"
    assert mock.call_args.kwargs["status_code"] == 200
    assert mock.call_args.kwargs["response_time"] > 0

    response = client.get("/foo/123")
    assert response.status_code == 200
    assert mock.call_count == 2
    assert mock.call_args is not None
    assert mock.call_args.kwargs["consumer"] == "test2"
    assert mock.call_args.kwargs["path"] == "/foo/{bar:str}"

    response = client.post("/bar")
    assert response.status_code == 201
    assert mock.call_count == 3
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "POST"

    response = client.get("/stream")
    assert response.status_code == 200
    assert mock.call_count == 4
    assert mock.call_args is not None
    assert mock.call_args.kwargs["response_size"] == 6

    response = client.get("/schema/openapi.json")
    assert response.status_code == 200
    assert mock.call_count == 4  # OpenAPI paths are filtered out


def test_middleware_requests_unhandled(client: TestClient, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")

    response = client.post("/xxx")
    assert response.status_code == 404
    mock.assert_not_called()


def test_middleware_requests_error(client: TestClient, mocker: MockerFixture):
    mock1 = mocker.patch("apitally.client.base.RequestCounter.add_request")
    mock2 = mocker.patch("apitally.client.base.ServerErrorCounter.add_server_error")

    response = client.post("/baz")
    assert response.status_code == 500
    mock1.assert_called_once()
    assert mock1.call_args is not None
    assert mock1.call_args.kwargs["method"] == "POST"
    assert mock1.call_args.kwargs["path"] == "/baz"
    assert mock1.call_args.kwargs["status_code"] == 500
    assert mock1.call_args.kwargs["response_time"] > 0

    mock2.assert_called_once()
    assert mock2.call_args is not None
    exception = mock2.call_args.kwargs["exception"]
    assert isinstance(exception, ValueError)


def test_middleware_validation_error(client: TestClient, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.ValidationErrorCounter.add_validation_errors")

    # Validation error as foo must be an integer
    response = client.get("/val?foo=bar")
    assert response.status_code == 400

    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "/val"
    assert len(mock.call_args.kwargs["detail"]) == 1
    assert mock.call_args.kwargs["detail"][0]["loc"] == ["query", "foo"]


def test_get_startup_data(app: Litestar, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.asyncio.ApitallyClient.set_startup_data")
    app.on_startup[0](app)  # type: ignore[call-arg]
    mock.assert_called_once()
    data = mock.call_args.args[0]
    assert len(data["openapi"]) > 0
    assert len(data["paths"]) == 6
    assert data["versions"]["litestar"]
    assert data["versions"]["app"] == "1.2.3"
    assert data["client"] == "python:litestar"
