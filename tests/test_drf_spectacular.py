from __future__ import annotations

import json
import sys
from importlib.util import find_spec

import pytest
from pytest_mock import MockerFixture


if find_spec("drf_spectacular") is None:
    pytest.skip("drf-spectacular is not available", allow_module_level=True)


@pytest.fixture(scope="module")
def reset_modules() -> None:
    for module in list(sys.modules):
        if (
            module.startswith("django.")
            or module.startswith("rest_framework.")
            or module.startswith("apitally.")
            or module == "tests.django_rest_framework_urls"
        ):
            del sys.modules[module]


@pytest.fixture(scope="module", autouse=True)
def setup(reset_modules, module_mocker: MockerFixture) -> None:
    import django
    from django.conf import settings

    module_mocker.patch("apitally.client.client_threading.ApitallyClient._instance", None)
    module_mocker.patch("apitally.client.client_threading.ApitallyClient.start_sync_loop")
    module_mocker.patch("apitally.client.client_threading.ApitallyClient.set_startup_data")
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
            "drf_spectacular",
        ],
        REST_FRAMEWORK={
            "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
        },
        APITALLY_MIDDLEWARE={
            "client_id": "76b5cb91-a0a4-4ea0-a894-57d2b9fcb2c9",
            "env": "dev",
        },
    )
    django.setup()


def test_get_startup_data():
    from apitally.django import _get_startup_data

    data = _get_startup_data(app_version="1.2.3", urlconfs=[None])
    openapi = json.loads(data["openapi"])
    assert len(data["paths"]) == 4
    assert len(openapi["paths"]) == 4
