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
    return "test"


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
    module_mocker.patch("apitally.client.threading.ApitallyClient.set_startup_data")
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
    assert mock.call_args.kwargs["consumer"] == "test"
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "/api/foo/{bar}"
    assert mock.call_args.kwargs["status_code"] == 200
    assert mock.call_args.kwargs["response_time"] > 0
    assert int(mock.call_args.kwargs["response_size"]) > 0

    response = client.post("/api/bar", data={"foo": "bar"})
    assert response.status_code == 200
    assert mock.call_count == 2
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "POST"
    assert int(mock.call_args.kwargs["request_size"]) > 0


def test_middleware_requests_404(client: Client, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")

    response = client.get("/api/none")
    assert response.status_code == 404
    mock.assert_not_called()


def test_middleware_requests_error(client: Client, mocker: MockerFixture):
    mock1 = mocker.patch("apitally.client.base.RequestCounter.add_request")
    mock2 = mocker.patch("apitally.client.base.ServerErrorCounter.add_server_error")

    response = client.put("/api/baz")
    assert response.status_code == 500
    mock1.assert_called_once()
    assert mock1.call_args is not None
    assert mock1.call_args.kwargs["method"] == "PUT"
    assert mock1.call_args.kwargs["path"] == "/api/baz"
    assert mock1.call_args.kwargs["status_code"] == 500
    assert mock1.call_args.kwargs["response_time"] > 0

    mock2.assert_called_once()
    assert mock2.call_args is not None
    exception = mock2.call_args.kwargs["exception"]
    assert isinstance(exception, ValueError)


def test_middleware_validation_error(client: Client, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.ValidationErrorCounter.add_validation_errors")

    response = client.get("/api/val?foo=bar")
    assert response.status_code == 422
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "/api/val"
    assert len(mock.call_args.kwargs["detail"]) == 1
    assert mock.call_args.kwargs["detail"][0]["loc"] == ["query", "foo"]


def test_get_startup_data():
    from apitally.django import _get_startup_data

    data = _get_startup_data(app_version="1.2.3", urlconfs=[None])
    openapi = json.loads(data["openapi"])
    assert len(data["paths"]) == 5
    assert len(openapi["paths"]) == 5

    assert data["versions"]["django"]
    assert data["versions"]["django-ninja"]
    assert data["versions"]["app"] == "1.2.3"
    assert data["client"] == "python:django"


def test_get_ninja_api_instances():
    from ninja import NinjaAPI

    from apitally.django import _get_ninja_api_instances

    apis = _get_ninja_api_instances()
    assert len(apis) == 1
    api = list(apis)[0]
    assert isinstance(api, NinjaAPI)


def test_get_ninja_api_endpoints():
    from apitally.django import _get_ninja_paths

    endpoints = _get_ninja_paths([None])
    assert len(endpoints) == 5
    assert all(len(e["summary"]) > 0 for e in endpoints)
    assert any(e["description"] is not None and len(e["description"]) > 0 for e in endpoints)


def test_check_import():
    from apitally.django import _check_import

    assert _check_import("ninja") is True
    assert _check_import("nonexistentpackage") is False
