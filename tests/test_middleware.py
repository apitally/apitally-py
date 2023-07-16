from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture


if TYPE_CHECKING:
    from starlette.applications import Starlette


def test_param_validation(app: Starlette, client_id: str):
    from starlette_apitally.middleware import ApitallyMiddleware

    with pytest.raises(ValueError):
        ApitallyMiddleware(app, client_id="76b5zb91-a0a4-4ea0-a894-57d2b9fcb2c9")
    with pytest.raises(ValueError):
        ApitallyMiddleware(app, client_id=client_id, env="invalid_string")
    with pytest.raises(ValueError):
        ApitallyMiddleware(app, client_id=client_id, app_version="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    with pytest.raises(ValueError):
        ApitallyMiddleware(app, client_id=client_id, send_every=1)


def test_success(app: Starlette, mocker: MockerFixture):
    from starlette.testclient import TestClient

    mock = mocker.patch("starlette_apitally.metrics.Metrics.log_request")
    client = TestClient(app)
    background_task_mock: MagicMock = app.state.background_task_mock  # type: ignore[attr-defined]

    response = client.get("/foo/")
    assert response.status_code == 200
    background_task_mock.assert_called_once()
    mock.assert_awaited_once()
    assert mock.await_args is not None
    assert mock.await_args.kwargs["method"] == "GET"
    assert mock.await_args.kwargs["path"] == "/foo/"
    assert mock.await_args.kwargs["status_code"] == 200
    assert mock.await_args.kwargs["response_time"] > 0

    response = client.get("/foo/123/")
    assert response.status_code == 200
    assert background_task_mock.call_count == 2
    assert mock.await_count == 2
    assert mock.await_args is not None
    assert mock.await_args.kwargs["path"] == "/foo/{bar}/"

    response = client.post("/bar/")
    assert response.status_code == 200
    assert mock.await_count == 3
    assert mock.await_args is not None
    assert mock.await_args.kwargs["method"] == "POST"


def test_error(app: Starlette, mocker: MockerFixture):
    from starlette.testclient import TestClient

    mocker.patch("starlette_apitally.client.ApitallyClient.send_app_info")
    mock = mocker.patch("starlette_apitally.metrics.Metrics.log_request")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/baz/")
    assert response.status_code == 500
    mock.assert_awaited_once()
    assert mock.await_args is not None
    assert mock.await_args.kwargs["method"] == "POST"
    assert mock.await_args.kwargs["path"] == "/baz/"
    assert mock.await_args.kwargs["status_code"] == 500
    assert mock.await_args.kwargs["response_time"] > 0


def test_unhandled(app: Starlette, mocker: MockerFixture):
    from starlette.testclient import TestClient

    mocker.patch("starlette_apitally.client.ApitallyClient.send_app_info")
    mock = mocker.patch("starlette_apitally.metrics.Metrics.log_request")
    client = TestClient(app)

    response = client.post("/xxx/")
    assert response.status_code == 404
    mock.assert_not_awaited()
