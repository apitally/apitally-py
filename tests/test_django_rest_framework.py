from __future__ import annotations

from importlib.util import find_spec
from typing import TYPE_CHECKING

import pytest
from pytest_mock import MockerFixture


if find_spec("rest_framework") is None:
    pytest.skip("django-rest-framework is not available", allow_module_level=True)

if TYPE_CHECKING:
    from rest_framework.test import APIClient


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
        },
    )
    django.setup()


@pytest.fixture(scope="module")
def client() -> APIClient:
    from rest_framework.test import APIClient

    return APIClient(raise_request_exception=False)


def test_middleware_requests_ok(client: APIClient, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")

    response = client.get("/foo/123/")
    assert response.status_code == 200
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "GET"
    assert mock.call_args.kwargs["path"] == "foo/<int:bar>/"
    assert mock.call_args.kwargs["status_code"] == 200
    assert mock.call_args.kwargs["response_time"] > 0
    assert int(mock.call_args.kwargs["response_size"]) > 0

    response = client.post("/bar/", data={"foo": "bar"})
    assert response.status_code == 200
    assert mock.call_count == 2
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "POST"
    assert int(mock.call_args.kwargs["request_size"]) > 0


def test_middleware_requests_error(client: APIClient, mocker: MockerFixture):
    mock = mocker.patch("apitally.client.base.RequestCounter.add_request")

    response = client.put("/baz/")
    assert response.status_code == 500
    mock.assert_called_once()
    assert mock.call_args is not None
    assert mock.call_args.kwargs["method"] == "PUT"
    assert mock.call_args.kwargs["path"] == "baz/"
    assert mock.call_args.kwargs["status_code"] == 500
    assert mock.call_args.kwargs["response_time"] > 0


def test_get_app_info():
    from django.urls import get_resolver

    from apitally.django import _extract_views_from_url_patterns, _get_app_info

    views = _extract_views_from_url_patterns(get_resolver().url_patterns)
    app_info = _get_app_info(views=views, app_version="1.2.3")
    assert len(app_info["paths"]) == 4
    assert app_info["versions"]["django"]
    assert app_info["versions"]["app"] == "1.2.3"
    assert app_info["client"] == "python:django"
