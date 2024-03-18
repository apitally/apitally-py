from __future__ import annotations

import json
import sys
from importlib.util import find_spec
from typing import TYPE_CHECKING, Optional

import pytest
from pytest_mock import MockerFixture


if find_spec("ninja") is None:
    pytest.skip("django-ninja is not available", allow_module_level=True)

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.test import Client


def identify_consumer(request: HttpRequest) -> Optional[str]:
    if consumer := request.GET.get("consumer"):
        return consumer
    return None


@pytest.fixture(scope="module")
def reset_modules() -> None:
    for module in list(sys.modules):
        if module.startswith("django.") or module.startswith("apitally."):
            del sys.modules[module]


@pytest.fixture(scope="module", autouse=True)
def setup(reset_modules, module_mocker: MockerFixture) -> None:
    import django
    from django.conf import settings

    module_mocker.patch("apitally.client.threading.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.threading.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.threading.ApitallyClient.set_app_info")
    module_mocker.patch("apitally.django.ApitallyMiddleware.config", None)

    settings.configure(
        ROOT_URLCONF="tests.django_ninja_urls",
        ALLOWED_HOSTS=["testserver"],
        SECRET_KEY="secret",
        MIDDLEWARE=[
            "apitally.django_ninja.ApitallyMiddleware",
            "django.middleware.common.CommonMiddleware",
        ],
        APITALLY_MIDDLEWARE={
            "client_id": "76b5cb91-a0a4-4ea0-a894-57d2b9fcb2c9",
            "env": "dev",
            "identify_consumer_callback": "tests.test_django_ninja.identify_consumer",
        },
    )
    django.setup()


@pytest.fixture(scope="module")
def client(module_mocker: MockerFixture) -> Client:
    import django
    from django.test import Client

    if django.VERSION[0] < 3:
        module_mocker.patch("django.test.client.Client.store_exc_info")  # Simulate raise_request_exception=False
    return Client(raise_request_exception=False)


def test_middleware_requests_ok(client: Client, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")

    response = client.get("/api/foo/123")
    assert response.status_code == 200
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "api/foo/<bar>"
    assert mock.call_args.kwargs["status_code"] == 200
    assert mock.call_args.kwargs["response_time"] > 0
    assert int(mock.call_args.kwargs["response_size"]) > 0

    response = client.post("/api/bar", data={"foo": "bar"})
    assert response.status_code == 200
    assert mock.call_count == 2
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "POST"
    assert int(mock.call_args.kwargs["request_size"]) > 0


def test_middleware_requests_error(client: Client, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")

    response = client.put("/api/baz")
    assert response.status_code == 500
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "PUT"
    assert mock.call_args.kwargs["path"] == "api/baz"
    assert mock.call_args.kwargs["status_code"] == 500
    assert mock.call_args.kwargs["response_time"] > 0


def test_middleware_validation_error(client: Client, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.ValidationErrorCounter.add_validation_errors")

    response = client.get("/api/val?foo=bar")
    assert response.status_code == 422
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "api/val"
    assert len(mock.call_args.kwargs["detail"]) == 1
    assert mock.call_args.kwargs["detail"][0]["loc"] == ["query", "foo"]


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
