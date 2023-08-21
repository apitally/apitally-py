from __future__ import annotations

from importlib.util import find_spec
from typing import TYPE_CHECKING, Iterator

import pytest
from pytest_mock import MockerFixture


if find_spec("rest_framework") is None:
    pytest.skip("django-rest-framework is not available", allow_module_level=True)

if TYPE_CHECKING:
    from rest_framework.test import APIClient


@pytest.fixture(scope="module", autouse=True)
def setup(module_mocker: MockerFixture) -> Iterator[None]:
    import django
    from django.conf import settings
    from django.utils.functional import empty

    module_mocker.patch("apitally.client.threading.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.threading.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.threading.ApitallyClient.send_app_info")

    settings.configure(
        ROOT_URLCONF="tests.django_urls",
        ALLOWED_HOSTS=["testserver"],
        SECRET_KEY="secret",
        MIDDLEWARE=[
            "apitally.django.ApitallyMiddleware",
        ],
        INSTALLED_APPS=[
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "rest_framework",
        ],
        APITALLY_MIDDLEWARE={
            "client_id": "76b5cb91-a0a4-4ea0-a894-57d2b9fcb2c9",
            "env": "default",
        },
    )
    django.setup()
    yield
    settings._wrapped = empty


@pytest.fixture(scope="module")
def client() -> APIClient:
    from rest_framework.test import APIClient

    return APIClient(raise_request_exception=False)


def test_middleware_requests_ok(client: APIClient, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestLogger.log_request")

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

    response = client.put("/baz/")
    assert response.status_code == 500
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "PUT"
    assert mock.call_args.kwargs["path"] == "baz/"
    assert mock.call_args.kwargs["status_code"] == 500
    assert mock.call_args.kwargs["response_time"] > 0


def test_get_app_info():
    from apitally.django import _get_app_info

    app_info = _get_app_info(app_version="1.2.3")
    assert len(app_info["paths"]) == 3
    assert app_info["versions"]["django"]
    assert app_info["versions"]["app"] == "1.2.3"
    assert app_info["framework"] == "django"
