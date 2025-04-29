from __future__ import annotations

from importlib.util import find_spec
from typing import TYPE_CHECKING, Optional

import pytest
from pytest_mock import MockerFixture

from .constants import CLIENT_ID, ENV


if find_spec("starlette") is None:
    pytest.skip("starlette is not available", allow_module_level=True)
else:
    # Need to import these at package level to avoid NameError in BlackSheep
    from blacksheep import Request, Response

if TYPE_CHECKING:
    from blacksheep import Application


@pytest.fixture(scope="module")
async def app(module_mocker: MockerFixture) -> Application:
    module_mocker.patch("apitally.client.client_asyncio.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.client_asyncio.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.client_asyncio.ApitallyClient.set_startup_data")
    module_mocker.patch("apitally.blacksheep.ApitallyMiddleware.delayed_set_startup_data")
    return get_app()


def get_app() -> Application:
    from blacksheep import Application, Router, text

    from apitally.blacksheep import ApitallyConsumer, RequestLoggingConfig, use_apitally

    def identify_consumer(request: Request) -> Optional[ApitallyConsumer]:
        return ApitallyConsumer("test", name="Test")

    router = Router(prefix="/api")
    app = Application(router=router)
    use_apitally(
        app,
        client_id=CLIENT_ID,
        env=ENV,
        request_logging_config=RequestLoggingConfig(
            enabled=True,
            log_request_body=True,
            log_response_body=True,
        ),
        identify_consumer_callback=identify_consumer,
    )

    @router.get("/foo")
    def foo() -> Response:
        return text("foo")

    @router.get("/foo/{bar}")
    def foo_bar(bar: str) -> Response:
        return text(f"foo: {bar}")

    @router.post("/bar")
    async def bar(request: Request) -> Response:
        body = await request.text()
        return text("bar: " + body)

    @router.post("/baz")
    def baz():
        raise ValueError("baz")

    return app


async def test_middleware_requests_ok(app: Application, mocker: MockerFixture):
    from blacksheep.testing import TestClient

    mock = mocker.patch("apitally.client.requests.RequestCounter.add_request")
    await app.start()
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
