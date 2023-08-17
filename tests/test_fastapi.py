from __future__ import annotations

from importlib.util import find_spec
from typing import TYPE_CHECKING

import pytest
from pytest_mock import MockerFixture


if find_spec("fastapi") is None:
    pytest.skip("fastapi is not available", allow_module_level=True)

if TYPE_CHECKING:
    from fastapi import FastAPI

from apitally.client.base import KeyInfo  # import here to avoid pydantic error


@pytest.fixture()
def app_with_auth() -> FastAPI:
    from fastapi import Depends, FastAPI, Security

    from apitally.fastapi import api_key_auth

    app = FastAPI()

    @app.get("/foo/")
    def foo(key: KeyInfo = Security(api_key_auth, scopes=["foo"])):
        return "foo"

    @app.get("/bar/")
    def bar(key: KeyInfo = Security(api_key_auth, scopes=["bar"])):
        return "bar"

    @app.get("/baz/", dependencies=[Depends(api_key_auth)])
    def baz():
        return "baz"

    return app


def test_api_key_auth(app_with_auth: FastAPI, mocker: MockerFixture):
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
    mock = mocker.patch("apitally.fastapi.ApitallyClient.get_instance")
    mock.return_value.key_registry = key_registry

    # Unauthenticated
    response = client.get("/foo")
    assert response.status_code == 401

    response = client.get("/baz")
    assert response.status_code == 401

    # Invalid API key
    response = client.get("/foo", headers={"Authorization": "ApiKey invalid"})
    assert response.status_code == 403

    # Valid API key with required scope
    response = client.get("/foo", headers=headers)
    assert response.status_code == 200

    # Valid API key, no scope required
    response = client.get("/baz", headers=headers)
    assert response.status_code == 200

    # Valid API key without required scope
    response = client.get("/bar", headers=headers)
    assert response.status_code == 403