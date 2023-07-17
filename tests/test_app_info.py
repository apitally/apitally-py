from __future__ import annotations

from typing import TYPE_CHECKING

from pytest_mock import MockerFixture


if TYPE_CHECKING:
    from starlette.applications import Starlette


def test_get_app_info(app: Starlette, mocker: MockerFixture):
    from starlette_apitally.app_info import get_app_info

    mocker.patch("starlette_apitally.middleware.ApitallyClient")
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()

    app_info = get_app_info(app=app.middleware_stack, app_version=None, openapi_url="/openapi.json")
    assert len(app_info["paths"]) == 4

    app_info = get_app_info(app=app.middleware_stack, app_version="1.2.3", openapi_url=None)
    assert app_info["info"]["version"] == "1.2.3"
    assert len(app_info["paths"]) == 4
    assert len(app_info["versions"]) > 0
