from __future__ import annotations

import json
from importlib.util import find_spec
from typing import TYPE_CHECKING, Optional

import pytest
from pytest_mock import MockerFixture


if find_spec("ninja") is None:
    pytest.skip("django-ninja is not available", allow_module_level=True)

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.test import Client

    from apitally.client.base import KeyRegistry


def identify_consumer(request: HttpRequest) -> Optional[str]:
    if consumer := request.GET.get("consumer"):
        return consumer
    return None


@pytest.fixture(scope="module", autouse=True)
def setup(module_mocker: MockerFixture) -> None:
    import django
    from django.apps.registry import apps
    from django.conf import settings
    from django.utils.functional import empty

    module_mocker.patch("apitally.client.threading.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.threading.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.threading.ApitallyClient.set_app_info")
    module_mocker.patch("apitally.django.ApitallyMiddleware.config", None)

    settings._wrapped = empty
    apps.app_configs.clear()
    apps.loading = False
    apps.ready = False

    settings.configure(
        ROOT_URLCONF="tests.django_ninja_urls",
        SECRET_KEY="secret",
        MIDDLEWARE=[
            "apitally.django_ninja.ApitallyMiddleware",
        ],
        APITALLY_MIDDLEWARE={
            "client_id": "76b5cb91-a0a4-4ea0-a894-57d2b9fcb2c9",
            "env": "default",
            "sync_api_keys": True,
            "identify_consumer_callback": "tests.test_django_ninja.identify_consumer",
        },
    )
    django.setup()


@pytest.fixture(scope="module")
def client() -> Client:
    from django.test import Client

    return Client(raise_request_exception=False)


def test_middleware_requests_ok(client: Client, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestLogger.log_request")
    mocker.patch("apitally.django_ninja.APIKeyAuth.authenticate")

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
    mocker.patch("apitally.django_ninja.APIKeyAuth.authenticate")

    response = client.put("/api/baz")
    assert response.status_code == 500
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "PUT"
    assert mock.call_args.kwargs["path"] == "api/baz"
    assert mock.call_args.kwargs["status_code"] == 500
    assert mock.call_args.kwargs["response_time"] > 0


def test_middleware_validation_error(client: Client, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.ValidationErrorLogger.log_validation_errors")
    mocker.patch("apitally.django_ninja.APIKeyAuth.authenticate")

    response = client.get("/api/val?foo=bar")
    assert response.status_code == 422
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "api/val"
    assert len(mock.call_args.kwargs["detail"]) == 1
    assert mock.call_args.kwargs["detail"][0]["loc"] == ["query", "foo"]


def test_api_key_auth(client: Client, key_registry: KeyRegistry, mocker: MockerFixture):
    client_get_instance_mock = mocker.patch("apitally.django_ninja.ApitallyClient.get_instance")
    client_get_instance_mock.return_value.key_registry = key_registry
    log_request_mock = mocker.patch("apitally.client.base.RequestLogger.log_request")

    # Unauthenticated
    response = client.get("/api/foo/123")
    assert response.status_code == 401

    # Invalid auth scheme
    headers = {"HTTP_AUTHORIZATION": "Bearer invalid"}
    response = client.get("/api/foo/123", **headers)  # type: ignore[arg-type]
    assert response.status_code == 401

    # Invalid API key
    headers = {"HTTP_AUTHORIZATION": "ApiKey invalid"}
    response = client.get("/api/foo/123", **headers)  # type: ignore[arg-type]
    assert response.status_code == 403

    # Invalid API key, custom header
    headers = {"HTTP_APIKEY": "invalid"}
    response = client.get("/api/foo", **headers)  # type: ignore[arg-type]
    assert response.status_code == 403

    # Valid API key, no scope required, custom header, consumer identified by API key
    headers = {"HTTP_APIKEY": "7ll40FB.DuHxzQQuGQU4xgvYvTpmnii7K365j9VI"}
    response = client.get("/api/foo", **headers)  # type: ignore[arg-type]
    assert response.status_code == 200
    assert log_request_mock.call_args.kwargs["consumer"] == "key:1"

    # Valid API key with required scope
    headers = {"HTTP_AUTHORIZATION": "ApiKey 7ll40FB.DuHxzQQuGQU4xgvYvTpmnii7K365j9VI"}
    response = client.get("/api/foo/123", **headers)  # type: ignore[arg-type]
    assert response.status_code == 200

    # Valid API key with required scope, consumer identified by custom function
    response = client.get("/api/foo/123?consumer=foo", **headers)  # type: ignore[arg-type]
    assert response.status_code == 200
    assert log_request_mock.call_args.kwargs["consumer"] == "foo"

    # Valid API key without required scope
    response = client.post("/api/bar", **headers)  # type: ignore[arg-type]
    assert response.status_code == 403

    # Valid API key, consumer identifier from request object
    response = client.put("/api/baz", **headers)  # type: ignore[arg-type]
    assert response.status_code == 500
    assert log_request_mock.call_args.kwargs["consumer"] == "baz"


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
