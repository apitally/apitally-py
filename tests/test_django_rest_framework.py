from __future__ import annotations

import json
import sys
from importlib.util import find_spec
from typing import TYPE_CHECKING

import pytest
from pytest_mock import MockerFixture


if find_spec("rest_framework") is None:
    pytest.skip("django-rest-framework is not available", allow_module_level=True)

if TYPE_CHECKING:
    from rest_framework.test import APIClient


@pytest.fixture(scope="module")
def reset_modules() -> None:
    for module in list(sys.modules):
        if module.startswith("django.") or module.startswith("rest_framework.") or module.startswith("apitally."):
            del sys.modules[module]


@pytest.fixture(scope="module", autouse=True)
def setup(reset_modules, module_mocker: MockerFixture) -> None:
    import django
    from django.conf import settings

    module_mocker.patch("apitally.client.threading.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.threading.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.threading.ApitallyClient.set_startup_data")
    module_mocker.patch("apitally.django.ApitallyMiddleware.config", None)

    settings.configure(
        ROOT_URLCONF="tests.django_rest_framework_urls",
        ALLOWED_HOSTS=["testserver"],
        SECRET_KEY="secret",
        MIDDLEWARE=[
            "apitally.django_rest_framework.ApitallyMiddleware",
            "django.middleware.common.CommonMiddleware",
        ],
        INSTALLED_APPS=[
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "rest_framework",
        ],
        APITALLY_MIDDLEWARE={
            "client_id": "76b5cb91-a0a4-4ea0-a894-57d2b9fcb2c9",
            "env": "dev",
            "urlconf": ["tests.django_rest_framework_urls"],
        },
    )
    django.setup()


@pytest.fixture(scope="module")
def client(module_mocker: MockerFixture) -> APIClient:
    import django
    from rest_framework.test import APIClient

    if django.VERSION[0] < 3:
        module_mocker.patch("django.test.client.Client.store_exc_info")  # Simulate raise_request_exception=False
    return APIClient(raise_request_exception=False)


def test_middleware_requests_ok(client: APIClient, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")

    response = client.get("/foo/123/")
    assert response.status_code == 200
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["consumer"] == "test"
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "/foo/{bar}/"
    assert mock.call_args.kwargs["status_code"] == 200
    assert mock.call_args.kwargs["response_time"] > 0
    assert int(mock.call_args.kwargs["response_size"]) > 0

    response = client.post("/bar/", data={"foo": "bar"})
    assert response.status_code == 200
    assert mock.call_count == 2
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "POST"
    assert int(mock.call_args.kwargs["request_size"]) > 0


def test_middleware_requests_404(client: APIClient, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")

    response = client.get("/api/none")
    assert response.status_code == 404
    mock.assert_not_called()


def test_middleware_requests_error(client: APIClient, mocker: MockerFixture):
    mock1 = mocker.patch("apitally.client.base.RequestCounter.add_request")
    mock2 = mocker.patch("apitally.client.base.ServerErrorCounter.add_server_error")

    response = client.put("/baz/")
    assert response.status_code == 500
    mock1.assert_called_once()
    assert mock1.call_args is not None
    assert mock1.call_args.kwargs["method"] == "PUT"
    assert mock1.call_args.kwargs["path"] == "/baz/"
    assert mock1.call_args.kwargs["status_code"] == 500
    assert mock1.call_args.kwargs["response_time"] > 0

    mock2.assert_called_once()
    assert mock2.call_args is not None
    exception = mock2.call_args.kwargs["exception"]
    assert isinstance(exception, ValueError)


def test_get_startup_data():
    from apitally.django import _get_startup_data

    data = _get_startup_data(app_version="1.2.3", urlconfs=[None])
    openapi = json.loads(data["openapi"])
    assert len(data["paths"]) == 4
    assert len(openapi["paths"]) == 4

    assert data["versions"]["django"]
    assert data["versions"]["djangorestframework"]
    assert data["versions"]["app"] == "1.2.3"
    assert data["client"] == "python:django"


def test_get_drf_api_endpoints():
    from apitally.django import _get_drf_paths

    endpoints = _get_drf_paths([None])
    assert len(endpoints) == 4
    assert endpoints[0]["method"] == "GET"
    assert endpoints[0]["path"] == "/foo/"
    assert endpoints[1]["path"] == "/foo/{bar}/"
