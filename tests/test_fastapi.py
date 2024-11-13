from __future__ import annotations

from importlib.util import find_spec
from typing import TYPE_CHECKING, Optional

import pytest
from pytest_mock import MockerFixture

from .constants import CLIENT_ID, ENV


if find_spec("fastapi") is None:
    pytest.skip("fastapi is not available", allow_module_level=True)

if TYPE_CHECKING:
    from fastapi import FastAPI

# Global imports to avoid NameErrors during FastAPI dependency injection
try:
    from fastapi import Request
except ImportError:
    pass


@pytest.fixture(scope="module")
def app(module_mocker: MockerFixture) -> FastAPI:
    from fastapi import FastAPI

    from apitally.fastapi import ApitallyMiddleware

    module_mocker.patch("apitally.client.client_asyncio.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.client_asyncio.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.client_asyncio.ApitallyClient.set_startup_data")
    module_mocker.patch("apitally.starlette.ApitallyMiddleware.delayed_set_startup_data")

    def identify_consumer(request: Request) -> Optional[str]:
        if consumer := request.query_params.get("consumer"):
            return consumer
        return None

    app = FastAPI()
    app.add_middleware(ApitallyMiddleware, client_id=CLIENT_ID, env=ENV, identify_consumer_callback=identify_consumer)

    @app.get("/foo/")
    def foo():
        return "foo"

    @app.get("/bar/")
    def bar():
        return "bar"

    @app.get("/baz/")
    def baz(request: Request):
        request.state.apitally_consumer = "baz"
        return "baz"

    return app


def test_get_openapi(app: FastAPI):
    from apitally.starlette import _get_openapi

    openapi = _get_openapi(app, "/openapi.json")
    assert openapi is not None
    assert len(openapi) > 0
