from __future__ import annotations

import json
from importlib.util import find_spec
from typing import TYPE_CHECKING, Iterator

import pytest
from pytest_mock import MockerFixture


if find_spec("ninja") is None:
    pytest.skip("django-ninja is not available", allow_module_level=True)

if TYPE_CHECKING:
    from django.test import Client

    from apitally.client.base import KeyRegistry


@pytest.fixture(scope="module", autouse=True)
def setup(module_mocker: MockerFixture) -> Iterator[None]:
    import django
    from django.apps.registry import Apps
    from django.conf import settings
    from django.utils.functional import empty

    module_mocker.patch("apitally.client.threading.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.threading.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.threading.ApitallyClient.send_app_info")
    module_mocker.patch("apitally.django.ApitallyMiddleware.config", None)

    module_mocker.patch("django.apps.registry.apps", Apps())

    settings.configure(
        ROOT_URLCONF="tests.django_ninja_urls",
        SECRET_KEY="secret",
        MIDDLEWARE=[
            "apitally.django_ninja.ApitallyMiddleware",
        ],
        APITALLY_MIDDLEWARE={
            "client_id": "76b5cb91-a0a4-4ea0-a894-57d2b9fcb2c9",
            "env": "default",
            "enable_keys": True,
        },
    )
    django.setup()
    yield
    settings._wrapped = empty


@pytest.fixture(scope="module")
def client() -> Client:
    from django.test import Client

    return Client(raise_request_exception=False)


def test_middleware_requests_ok(client: Client, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestLogger.log_request")
    mocker.patch("apitally.django_ninja.AuthorizationAPIKeyHeader.authenticate")

    response = client.get("/api/foo/123")
    assert response.status_code == 200
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "api/foo/<bar>"
    assert mock.call_args.kwargs["status_code"] == 200
    assert mock.call_args.kwargs["response_time"] > 0

    response = client.post("/api/bar")
    assert response.status_code == 200
    assert mock.call_count == 2
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "POST"


def test_middleware_requests_error(client: Client, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestLogger.log_request")
    mocker.patch("apitally.django_ninja.AuthorizationAPIKeyHeader.authenticate")

    response = client.put("/api/baz")
    assert response.status_code == 500
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "PUT"
    assert mock.call_args.kwargs["path"] == "api/baz"
    assert mock.call_args.kwargs["status_code"] == 500
    assert mock.call_args.kwargs["response_time"] > 0


def test_api_key_auth(client: Client, key_registry: KeyRegistry, mocker: MockerFixture):
    mock = mocker.patch("apitally.django_ninja.ApitallyClient.get_instance")
    mock.return_value.key_registry = key_registry

    # Unauthenticated
    response = client.get("/api/foo/123")
    assert response.status_code == 401

    # Invalid auth scheme
    headers = {"Authorization": "Bearer invalid"}
    response = client.get("/api/foo/123", headers=headers)  # type: ignore[arg-type]
    assert response.status_code == 401

    # Invalid API key
    headers = {"Authorization": "ApiKey invalid"}
    response = client.get("/api/foo/123", headers=headers)  # type: ignore[arg-type]
    assert response.status_code == 403

    # Valid API key, no scope required
    headers = {"Authorization": "ApiKey 7ll40FB.DuHxzQQuGQU4xgvYvTpmnii7K365j9VI"}
    response = client.get("/api/foo", headers=headers)  # type: ignore[arg-type]
    assert response.status_code == 200

    # Valid API key with required scope
    response = client.get("/api/foo/123", headers=headers)  # type: ignore[arg-type]
    assert response.status_code == 200

    # Valid API key without required scope
    response = client.post("/api/bar", headers=headers)  # type: ignore[arg-type]
    assert response.status_code == 403


def test_get_app_info(mocker: MockerFixture):
    from django.urls import get_resolver

    from apitally.django import _extract_views_from_url_patterns, _get_app_info

    views = _extract_views_from_url_patterns(get_resolver().url_patterns)

    app_info = _get_app_info(views=views)
    openapi = json.loads(app_info["openapi"])
    assert len(app_info["paths"]) == len(openapi["paths"])

    app_info = _get_app_info(views=views, app_version="1.2.3", openapi_url="/api/openapi.json")
    assert "openapi" in app_info
    assert app_info["versions"]["app"] == "1.2.3"
