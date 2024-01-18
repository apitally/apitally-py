from __future__ import annotations

from importlib.util import find_spec
from typing import TYPE_CHECKING

import pytest
from pytest_mock import MockerFixture


if find_spec("rest_framework") is None:
    pytest.skip("django-rest-framework is not available", allow_module_level=True)

if TYPE_CHECKING:
    from rest_framework.test import APIClient

    from apitally.client.base import KeyRegistry


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
        ROOT_URLCONF="tests.django_rest_framework_urls",
        ALLOWED_HOSTS=["testserver"],
        SECRET_KEY="secret",
        MIDDLEWARE=[
            "apitally.django_rest_framework.ApitallyMiddleware",
        ],
        INSTALLED_APPS=[
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "rest_framework",
        ],
        APITALLY_MIDDLEWARE={
            "client_id": "76b5cb91-a0a4-4ea0-a894-57d2b9fcb2c9",
            "env": "dev",
        },
    )
    django.setup()


@pytest.fixture(scope="module")
def client() -> APIClient:
    from rest_framework.test import APIClient

    return APIClient(raise_request_exception=False)


def test_middleware_requests_ok(client: APIClient, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestLogger.log_request")
    mocker.patch("apitally.django_rest_framework.HasAPIKey.has_permission", return_value=True)

    response = client.get("/foo/123/")
    assert response.status_code == 200
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "foo/<int:bar>/"
    assert mock.call_args.kwargs["status_code"] == 200
    assert mock.call_args.kwargs["response_time"] > 0

    response = client.post("/bar/")
    assert response.status_code == 200
    assert mock.call_count == 2
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "POST"


def test_middleware_requests_error(client: APIClient, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestLogger.log_request")
    mocker.patch("apitally.django_rest_framework.HasAPIKey.has_permission", return_value=True)

    response = client.put("/baz/")
    assert response.status_code == 500
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "PUT"
    assert mock.call_args.kwargs["path"] == "baz/"
    assert mock.call_args.kwargs["status_code"] == 500
    assert mock.call_args.kwargs["response_time"] > 0


def test_api_key_auth(client: APIClient, key_registry: KeyRegistry, mocker: MockerFixture):
    client_get_instance_mock = mocker.patch("apitally.django_rest_framework.ApitallyClient.get_instance")
    client_get_instance_mock.return_value.key_registry = key_registry
    log_request_mock = mocker.patch("apitally.client.base.RequestLogger.log_request")

    # Unauthenticated
    response = client.get("/foo/123/")
    assert response.status_code == 403

    # Invalid auth scheme
    headers = {"HTTP_AUTHORIZATION": "Bearer invalid"}
    response = client.get("/foo/123/", **headers)  # type: ignore[arg-type]
    assert response.status_code == 403

    # Invalid API key
    headers = {"HTTP_AUTHORIZATION": "ApiKey invalid"}
    response = client.get("/foo/123/", **headers)  # type: ignore[arg-type]
    assert response.status_code == 403

    # Valid API key, no scope required
    headers = {"HTTP_AUTHORIZATION": "ApiKey 7ll40FB.DuHxzQQuGQU4xgvYvTpmnii7K365j9VI"}
    response = client.get("/foo/", **headers)  # type: ignore[arg-type]
    assert response.status_code == 200
    assert log_request_mock.call_args.kwargs["consumer"] == "key:1"

    # Valid API key with required scope
    response = client.get("/foo/123/", **headers)  # type: ignore[arg-type]
    assert response.status_code == 200

    # Valid API key without required scope
    response = client.post("/bar/", **headers)  # type: ignore[arg-type]
    assert response.status_code == 403

    # Valid API key, custom header
    mocker.patch.dict("django.conf.settings.__dict__", {"APITALLY_CUSTOM_API_KEY_HEADER": "ApiKey"})
    headers = {"HTTP_APIKEY": "7ll40FB.DuHxzQQuGQU4xgvYvTpmnii7K365j9VI"}
    response = client.get("/foo/", **headers)  # type: ignore[arg-type]
    assert response.status_code == 200

    # Invalid API key, custom header
    mocker.patch.dict("django.conf.settings.__dict__", {"APITALLY_CUSTOM_API_KEY_HEADER": "ApiKey"})
    headers = {"HTTP_APIKEY": "invalid"}
    response = client.get("/foo/", **headers)  # type: ignore[arg-type]
    assert response.status_code == 403


def test_get_app_info():
    from django.urls import get_resolver

    from apitally.django import _extract_views_from_url_patterns, _get_app_info

    views = _extract_views_from_url_patterns(get_resolver().url_patterns)
    app_info = _get_app_info(views=views, app_version="1.2.3")
    assert len(app_info["paths"]) == 4
    assert app_info["versions"]["django"]
    assert app_info["versions"]["app"] == "1.2.3"
    assert app_info["client"] == "python:django"
