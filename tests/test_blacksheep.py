from __future__ import annotations

from importlib.util import find_spec
from typing import Optional

import pytest
from pytest_mock import MockerFixture

from .constants import CLIENT_ID, ENV


if find_spec("blacksheep") is None:
    pytest.skip("blacksheep is not available", allow_module_level=True)
else:
    # Need to import these at package level to avoid NameError in BlackSheep
    from blacksheep import Application, Request, Response


@pytest.fixture(scope="module")
async def app(module_mocker: MockerFixture) -> Application:
    module_mocker.patch("apitally.client.client_asyncio.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.client_asyncio.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.client_asyncio.ApitallyClient.set_startup_data")
    app = get_app()
    await app.start()
    return app


def get_app() -> Application:
    from blacksheep import StreamedContent, get, post, text

    from apitally.blacksheep import ApitallyConsumer, use_apitally

    def identify_consumer(request: Request) -> Optional[ApitallyConsumer]:
        return ApitallyConsumer("test", name="Test")

    app = Application(show_error_details=True)

    use_apitally(
        app,
        client_id=CLIENT_ID,
        env=ENV,
        app_version="1.2.3",
        enable_request_logging=True,
        log_request_body=True,
        log_response_body=True,
        consumer_callback=identify_consumer,
    )

    @get("/api/foo")
    def foo() -> Response:
        return text("foo")

    @get("/api/foo/{bar}")
    def foo_bar(bar: str) -> Response:
        return text(f"foo: {bar}")

    @post("/api/bar")
    async def bar(request: Request) -> Response:
        body = await request.text()
        return text("bar: " + body)

    @post("/api/baz")
    def baz():
        raise ValueError("baz")

    @get("/api/stream")
    async def stream() -> Response:
        async def stream_response():
            yield b"foo"
            yield b"bar"

        return Response(200, content=StreamedContent(b"text/plain", stream_response))

    return app


async def test_middleware_requests_ok(app: Application, mocker: MockerFixture):
    from blacksheep.testing import TestClient

    mock = mocker.patch("apitally.client.requests.RequestCounter.add_request")
    client = TestClient(app)

    response = await client.get("/api/foo")
    assert response.status == 200
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["consumer"] == "test"
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "/api/foo"
    assert mock.call_args.kwargs["status_code"] == 200
    assert mock.call_args.kwargs["response_time"] > 0

    response = await client.get("/api/foo/123")
    assert response.status == 200
    assert mock.call_count == 2
    assert mock.call_args is not None
    assert mock.call_args.kwargs["path"] == "/api/foo/{bar}"

    response = await client.post("/api/bar")
    assert response.status == 200
    assert mock.call_count == 3
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "POST"

    response = await client.get("/api/stream")
    assert response.status == 200
    assert mock.call_count == 4
    assert mock.call_args is not None
    assert mock.call_args.kwargs["response_size"] == 6


async def test_middleware_requests_error(app: Application, mocker: MockerFixture):
    from blacksheep.testing import TestClient

    mock1 = mocker.patch("apitally.client.requests.RequestCounter.add_request")
    mock2 = mocker.patch("apitally.client.server_errors.ServerErrorCounter.add_server_error")
    client = TestClient(app)

    response = await client.post("/api/baz")
    assert response.status == 500
    mock1.assert_called_once()
    assert mock1.call_args is not None
    assert mock1.call_args.kwargs["method"] == "POST"
    assert mock1.call_args.kwargs["path"] == "/api/baz"
    assert mock1.call_args.kwargs["status_code"] == 500
    assert mock1.call_args.kwargs["response_time"] > 0

    mock2.assert_called_once()
    assert mock2.call_args is not None
    exception = mock2.call_args.kwargs["exception"]
    assert isinstance(exception, ValueError)


async def test_middleware_requests_unhandled(app: Application, mocker: MockerFixture):
    from blacksheep.testing import TestClient

    mock = mocker.patch("apitally.client.requests.RequestCounter.add_request")
    client = TestClient(app)

    response = await client.post("/xxx")
    assert response.status == 404
    mock.assert_not_called()


async def test_middleware_request_logging(app: Application, mocker: MockerFixture):
    from blacksheep.testing import TestClient, TextContent

    from apitally.client.request_logging import BODY_TOO_LARGE

    mock = mocker.patch("apitally.client.request_logging.RequestLogger.log_request")
    client = TestClient(app)

    response = await client.get("/api/foo/123", query="foo=bar", headers={"test-header": "test"})
    assert response.status == 200
    assert await response.text() == "foo: 123"
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["request"]["method"] == "GET"
    assert mock.call_args.kwargs["request"]["path"] == "/api/foo/{bar}"
    assert mock.call_args.kwargs["request"]["url"] == "http://127.0.0.1:8000/api/foo/123?foo=bar"
    assert ("test-header", "test") in mock.call_args.kwargs["request"]["headers"]
    assert mock.call_args.kwargs["request"]["consumer"] == "test"
    assert mock.call_args.kwargs["response"]["status_code"] == 200
    assert mock.call_args.kwargs["response"]["response_time"] > 0
    assert mock.call_args.kwargs["response"]["size"] > 0
    assert mock.call_args.kwargs["response"]["body"] == b"foo: 123"

    response = await client.post("/api/bar", content=TextContent("foo"))
    assert response.status == 200
    assert mock.call_count == 2
    assert mock.call_args is not None
    assert mock.call_args.kwargs["request"]["method"] == "POST"
    assert mock.call_args.kwargs["request"]["path"] == "/api/bar"
    assert mock.call_args.kwargs["request"]["url"] == "http://127.0.0.1:8000/api/bar"
    assert mock.call_args.kwargs["request"]["body"] == b"foo"
    assert mock.call_args.kwargs["response"]["body"] == b"bar: foo"

    mocker.patch("apitally.blacksheep.MAX_BODY_SIZE", 2)
    response = await client.post("/api/bar", content=TextContent("foo"), headers={"Content-Length": "3"})
    assert response.status == 200
    assert mock.call_count == 3
    assert mock.call_args is not None
    assert mock.call_args.kwargs["request"]["body"] == BODY_TOO_LARGE


def test_get_startup_data(app: Application, mocker: MockerFixture):
    from apitally.blacksheep import _get_startup_data

    mocker.patch("apitally.blacksheep.ApitallyClient")

    data = _get_startup_data(app, app_version="1.2.3")
    assert len(data["paths"]) == 5
    assert {"method": "GET", "path": "/api/foo"} in data["paths"]
    assert {"method": "GET", "path": "/api/foo/{bar}"} in data["paths"]
    assert {"method": "POST", "path": "/api/bar"} in data["paths"]
    assert {"method": "POST", "path": "/api/baz"} in data["paths"]
    assert {"method": "GET", "path": "/api/stream"} in data["paths"]
    assert data["versions"]["blacksheep"]
    assert data["versions"]["app"] == "1.2.3"
    assert data["client"] == "python:blacksheep"
