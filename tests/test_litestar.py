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

    from apitally.litestar import ApitallyConsumer, ApitallyPlugin, RequestLoggingConfig

    async def mocked_handle_shutdown(_):
        # Empty function instead of Mock to avoid the following error in Python 3.10:
        # TypeError: 'Mock' object is not subscriptable
        pass

    module_mocker.patch("apitally.client.client_asyncio.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.client_asyncio.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.client_asyncio.ApitallyClient.set_startup_data")
    module_mocker.patch("apitally.client.client_asyncio.ApitallyClient.handle_shutdown", mocked_handle_shutdown)

    @get("/foo")
    async def foo() -> str:
        return "foo"

    @get("/foo/{bar:str}")
    async def foo_bar(request: Request, bar: str) -> str:
        request.state.apitally_consumer = "test2"
        return f"foo: {bar}"

    @post("/bar")
    async def bar(request: Request) -> str:
        body = await request.body()
        return "bar: " + body.decode()

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
        request_logging_config=RequestLoggingConfig(
            enabled=True,
            log_request_body=True,
            log_response_body=True,
        ),
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
    mock = mocker.patch("apitally.client.requests.RequestCounter.add_request")

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
    mock = mocker.patch("apitally.client.requests.RequestCounter.add_request")

    response = client.post("/xxx")
    assert response.status_code == 404
    mock.assert_not_called()


def test_middleware_requests_error(client: TestClient, mocker: MockerFixture):
    mock1 = mocker.patch("apitally.client.requests.RequestCounter.add_request")
    mock2 = mocker.patch("apitally.client.server_errors.ServerErrorCounter.add_server_error")

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
    mock = mocker.patch("apitally.client.validation_errors.ValidationErrorCounter.add_validation_errors")

    # Validation error as foo must be an integer
    response = client.get("/val?foo=bar")
    assert response.status_code == 400

    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "/val"
    assert len(mock.call_args.kwargs["detail"]) == 1
    assert mock.call_args.kwargs["detail"][0]["loc"] == ["query", "foo"]


def test_middleware_request_logging(client: TestClient, mocker: MockerFixture):
    from apitally.client.request_logging import BODY_TOO_LARGE

    mock = mocker.patch("apitally.client.request_logging.RequestLogger.log_request")

    response = client.get("/foo/123?foo=bar", headers={"Test-Header": "test"})
    assert response.status_code == 200
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["request"]["method"] == "GET"
    assert mock.call_args.kwargs["request"]["path"] == "/foo/{bar:str}"
    assert mock.call_args.kwargs["request"]["url"] == "http://testserver.local/foo/123?foo=bar"
    assert ("test-header", "test") in mock.call_args.kwargs["request"]["headers"]
    assert mock.call_args.kwargs["request"]["consumer"] == "test2"
    assert mock.call_args.kwargs["response"]["status_code"] == 200
    assert mock.call_args.kwargs["response"]["response_time"] > 0
    assert ("content-type", "text/plain; charset=utf-8") in mock.call_args.kwargs["response"]["headers"]
    assert mock.call_args.kwargs["response"]["size"] > 0

    response = client.post("/bar", content=b"foo")
    assert response.status_code == 201
    assert mock.call_count == 2
    assert mock.call_args is not None
    assert mock.call_args.kwargs["request"]["method"] == "POST"
    assert mock.call_args.kwargs["request"]["path"] == "/bar"
    assert mock.call_args.kwargs["request"]["url"] == "http://testserver.local/bar"
    assert mock.call_args.kwargs["request"]["body"] == b"foo"
    assert mock.call_args.kwargs["response"]["body"] == b"bar: foo"

    mocker.patch("apitally.litestar.MAX_BODY_SIZE", 2)
    response = client.post("/bar", content=b"foo")
    assert response.status_code == 201
    assert mock.call_count == 3
    assert mock.call_args is not None
    assert mock.call_args.kwargs["request"]["body"] == BODY_TOO_LARGE
    assert mock.call_args.kwargs["response"]["body"] == BODY_TOO_LARGE


def test_get_startup_data(app: Litestar, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.client_asyncio.ApitallyClient.set_startup_data")
    app.on_startup[0](app)  # type: ignore[call-arg]
    mock.assert_called_once()
    data = mock.call_args.args[0]
    assert len(data["openapi"]) > 0
    assert len(data["paths"]) == 6
    assert data["versions"]["litestar"]
    assert data["versions"]["app"] == "1.2.3"
    assert data["client"] == "python:litestar"
