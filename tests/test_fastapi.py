from __future__ import annotations

from importlib.util import find_spec
from typing import TYPE_CHECKING

import pytest
from pytest_mock import MockerFixture


if find_spec("fastapi") is None:
    pytest.skip("fastapi is not available", allow_module_level=True)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from apitally.client.base import KeyRegistry

from apitally.client.base import KeyInfo  # import here to avoid pydantic error


CLIENT_ID = "76b5cb91-a0a4-4ea0-a894-57d2b9fcb2c9"
ENV = "default"


@pytest.fixture()
def app(mocker: MockerFixture) -> FastAPI:
    from fastapi import Depends, FastAPI, Security

    from apitally.fastapi import APIKeyAuth, ApitallyMiddleware, api_key_auth

    mocker.patch("apitally.client.asyncio.ApitallyClient._instance", None)

    app = FastAPI()
    app.add_middleware(ApitallyMiddleware, client_id=CLIENT_ID, env=ENV)
    api_key_auth_custom = APIKeyAuth(custom_header="ApiKey")

    @app.get("/foo/")
    def foo(key: KeyInfo = Security(api_key_auth, scopes=["foo"])):
        return "foo"

    @app.get("/bar/")
    def bar(key: KeyInfo = Security(api_key_auth, scopes=["bar"])):
        return "bar"

    @app.get("/baz/", dependencies=[Depends(api_key_auth_custom)])
    def baz():
        return "baz"

    return app


def test_api_key_auth(app: FastAPI, key_registry: KeyRegistry, mocker: MockerFixture):
    from starlette.testclient import TestClient

    client = TestClient(app)
    headers = {"Authorization": "ApiKey 7ll40FB.DuHxzQQuGQU4xgvYvTpmnii7K365j9VI"}
    headers_custom = {"ApiKey": "7ll40FB.DuHxzQQuGQU4xgvYvTpmnii7K365j9VI"}
    client_get_instance_mock = mocker.patch("apitally.fastapi.ApitallyClient.get_instance")
    client_get_instance_mock.return_value.key_registry = key_registry
    log_request_mock = mocker.patch("apitally.client.base.RequestLogger.log_request")

    # Unauthenticated
    response = client.get("/foo")
    assert response.status_code == 401

    # Unauthenticated, custom header
    response = client.get("/baz")
    assert response.status_code == 403

    # Invalid auth scheme
    response = client.get("/foo", headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 401

    # Invalid API key
    response = client.get("/foo", headers={"Authorization": "ApiKey invalid"})
    assert response.status_code == 403

    # Invalid API key, custom header
    response = client.get("/baz", headers={"ApiKey": "invalid"})
    assert response.status_code == 403

    # Valid API key with required scope
    response = client.get("/foo", headers=headers)
    assert response.status_code == 200
    assert log_request_mock.call_args.kwargs["consumer"] == "key:1"

    # Valid API key, no scope required, custom header
    response = client.get("/baz", headers=headers_custom)
    assert response.status_code == 200

    # Valid API key without required scope
    response = client.get("/bar", headers=headers)
    assert response.status_code == 403
