from __future__ import annotations

from importlib.util import find_spec
from typing import TYPE_CHECKING, Optional

import pytest
from pytest_mock import MockerFixture

from .constants import CLIENT_ID, ENV


if find_spec("litestar") is None:
    pytest.skip("litestar is not available", allow_module_level=True)

if TYPE_CHECKING:
    from litestar.app import Litestar


@pytest.fixture(scope="module")
async def app(module_mocker: MockerFixture) -> Litestar:
    from litestar.app import Litestar
    from litestar.connection import Request
    from litestar.handlers import get, post

    from apitally.litestar import ApitallyPlugin

    module_mocker.patch("apitally.client.asyncio.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.asyncio.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.asyncio.ApitallyClient.set_app_info")

    @get("/foo")
    async def foo() -> str:
        return "foo"

    @get("/foo/{bar:str}")
    async def foo_bar(bar: str) -> str:
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

    def identify_consumer(request: Request) -> Optional[str]:
        return "test"

    plugin = ApitallyPlugin(
        client_id=CLIENT_ID,
        env=ENV,
        app_version="1.2.3",
        identify_consumer_callback=identify_consumer,
    )
    app = Litestar(
        route_handlers=[foo, foo_bar, bar, baz, val],
        plugins=[plugin],
    )
    return app


def test_middleware_requests_ok(app: Litestar, mocker: MockerFixture):
    from litestar.testing import TestClient

    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")
    client = TestClient(app)

    response = client.get("/foo/")
    assert response.status_code == 200
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["consumer"] == "test"
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "/foo"
    assert mock.call_args.kwargs["status_code"] == 200
    assert mock.call_args.kwargs["response_time"] > 0

    response = client.get("/foo/123")
    assert response.status_code == 200
    assert mock.call_count == 2
    assert mock.call_args is not None
    assert mock.call_args.kwargs["path"] == "/foo/{bar:str}"

    response = client.post("/bar")
    assert response.status_code == 201
    assert mock.call_count == 3
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "POST"


def test_middleware_requests_unhandled(app: Litestar, mocker: MockerFixture):
    from litestar.testing import TestClient

    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")
    client = TestClient(app)

    response = client.post("/xxx")
    assert response.status_code == 404
    mock.assert_not_called()


def test_middleware_requests_error(app: Litestar, mocker: MockerFixture):
    from litestar.testing import TestClient

    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/baz")
    assert response.status_code == 500
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "POST"
    assert mock.call_args.kwargs["path"] == "/baz"
    assert mock.call_args.kwargs["status_code"] == 500
    assert mock.call_args.kwargs["response_time"] > 0


def test_middleware_validation_error(app: Litestar, mocker: MockerFixture):
    from litestar.testing import TestClient

    mock = mocker.patch("apitally.client.base.ValidationErrorCounter.add_validation_errors")
    client = TestClient(app)

    # Validation error as foo must be an integer
    response = client.get("/val?foo=bar")
    assert response.status_code == 400

    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "/val"
    assert len(mock.call_args.kwargs["detail"]) == 1
    assert mock.call_args.kwargs["detail"][0]["loc"] == ["query", "foo"]


def test_get_app_info(app: Litestar, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.asyncio.ApitallyClient.set_app_info")
    app.on_startup[0](app)  # type: ignore[call-arg]
    mock.assert_called_once()
    app_info = mock.call_args.args[0]
    assert len(app_info["openapi"]) > 0
    assert len(app_info["paths"]) == 5
    assert app_info["versions"]["litestar"]
    assert app_info["versions"]["app"] == "1.2.3"
    assert app_info["client"] == "python:litestar"
