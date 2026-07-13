import json
import platform
from importlib import metadata
from typing import Any

import pytest
from litestar import Litestar, Router, get, post
from litestar.exceptions import HTTPException
from litestar.middleware.base import DefineMiddleware
from litestar.params import FromPath
from litestar.plugins.opentelemetry import (
    OpenTelemetryConfig,
    OpenTelemetryInstrumentationMiddleware,
    OpenTelemetryPlugin,
)
from litestar.testing import TestClient
from opentelemetry.trace import SpanKind, StatusCode

from apitally.litestar import ApitallyPlugin
from apitally.shared import activation, config
from apitally.shared.redaction import REDACTED
from tests.conftest import (
    WRITE_TOKEN,
    InMemoryExporters,
    attach_metric_reader,
    duration_data_points,
    exported_spans,
    startup_payload,
)


@get("/users/{user_id:int}")
async def get_user(user_id: FromPath[int]) -> dict[str, int]:
    return {"id": user_id}


@post("/users")
async def create_user(data: dict[str, Any]) -> dict[str, Any]:
    return data


@get("/healthz")
async def healthz() -> str:
    return "ok"


@get("/error")
async def error_route() -> None:
    raise ValueError("boom")


@get("/bad-request")
async def bad_request_route() -> None:
    raise HTTPException(status_code=400, detail="invalid")


ROUTE_HANDLERS = [get_user, create_user, healthz]


def make_app(
    monkeypatch: pytest.MonkeyPatch,
    plugins: list[Any] | None = None,
    middleware: list[Any] | None = None,
    **plugin_kwargs: Any,
) -> Litestar:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    return Litestar(
        route_handlers=ROUTE_HANDLERS,
        plugins=[*(plugins or []), ApitallyPlugin(write_token=WRITE_TOKEN, **plugin_kwargs)],
        middleware=middleware or [],
    )


def test_route_repair_metrics_and_no_noise_spans(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    with TestClient(app=make_app(monkeypatch)) as client:
        reader = attach_metric_reader()
        assert client.get("/users/123").status_code == 200

    (span,) = exported_spans(exporters)
    assert span.kind == SpanKind.SERVER
    assert span.name == "GET /users/{user_id}"
    assert (span.attributes or {})["http.route"] == "/users/{user_id}"

    points = [dict(point.attributes or {}) for point in duration_data_points(reader)]
    assert len(points) == 1
    assert points[0]["http.route"] == "/users/{user_id}"


@pytest.mark.parametrize(
    "make_app_kwargs",
    [
        pytest.param({"plugins": [OpenTelemetryPlugin(OpenTelemetryConfig())]}, id="plugin"),
        pytest.param(
            {"middleware": [DefineMiddleware(OpenTelemetryInstrumentationMiddleware, config=OpenTelemetryConfig())]},
            id="legacy-middleware",
        ),
    ],
)
def test_user_otel_setup_detected_and_repaired(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch, make_app_kwargs: dict[str, Any]
):
    app = make_app(monkeypatch, **make_app_kwargs)
    assert sum(isinstance(plugin, OpenTelemetryPlugin) for plugin in app.plugins.init) == 1
    with TestClient(app=app) as client:
        assert client.get("/users/123").status_code == 200

    # The user's config has no exclude_spans; the span processor's built-in filter drops the receive/send spans
    (span,) = exported_spans(exporters)
    assert span.kind == SpanKind.SERVER
    assert span.name == "GET /users/{user_id}"
    assert (span.attributes or {})["http.route"] == "/users/{user_id}"


def test_excluded_request_exports_nothing(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    with TestClient(app=make_app(monkeypatch)) as client:
        assert client.get("/healthz").status_code == 200

    assert exported_spans(exporters) == []


def test_request_and_response_bodies_captured_and_redacted(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    with TestClient(app=make_app(monkeypatch, log_request_body=True, log_response_body=True)) as client:
        response = client.post("/users", json={"user": "u", "password": "secret"})
        assert response.status_code == 201

    (span,) = exported_spans(exporters)
    attributes = span.attributes or {}
    assert json.loads(str(attributes["apitally.request.body"])) == {"user": "u", "password": REDACTED}
    assert json.loads(str(attributes["apitally.response.body"])) == {"user": "u", "password": REDACTED}


def test_headers_captured_for_unmatched_requests(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    app = make_app(monkeypatch, log_request_headers=True, log_response_headers=True)
    with TestClient(app=app) as client:
        assert client.get("/nonexistent", headers={"X-Test": "1"}).status_code == 404
        assert client.head("/users/123", headers={"X-Test": "1"}).status_code == 405

    spans = exported_spans(exporters)
    assert len(spans) == 2
    for span in spans:
        attributes = span.attributes or {}
        assert attributes["http.route"] == ""
        assert attributes["http.request.header.x-test"] == ["1"]
        assert attributes["http.response.header.content-type"] == ["application/json"]


def test_unhandled_exception_recorded_on_server_span(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    app = Litestar(route_handlers=[error_route], plugins=[ApitallyPlugin(write_token=WRITE_TOKEN)])
    with TestClient(app=app) as client:
        assert client.get("/error").status_code == 500

    (span,) = exported_spans(exporters)
    assert span.kind == SpanKind.SERVER
    assert span.status.status_code == StatusCode.ERROR
    assert (span.attributes or {})["http.response.status_code"] == 500
    (event,) = [event for event in span.events if event.name == "exception"]
    assert (event.attributes or {})["exception.type"] == "ValueError"
    assert (event.attributes or {})["exception.message"] == "boom"


def test_route_includes_router_path_prefix(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    router = Router(path="/v1", route_handlers=[get_user])
    app = Litestar(route_handlers=[router], plugins=[ApitallyPlugin(write_token=WRITE_TOKEN)])
    with TestClient(app=app) as client:
        reader = attach_metric_reader()
        assert client.get("/v1/users/123").status_code == 200

    (span,) = exported_spans(exporters)
    (point,) = duration_data_points(reader)
    assert (span.attributes or {})["http.route"] == "/v1/users/{user_id}"
    assert (point.attributes or {})["http.route"] == "/v1/users/{user_id}"


def test_client_error_not_recorded_as_exception(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    app = Litestar(route_handlers=[bad_request_route], plugins=[ApitallyPlugin(write_token=WRITE_TOKEN)])
    with TestClient(app=app) as client:
        assert client.get("/bad-request").status_code == 400

    (span,) = exported_spans(exporters)
    assert (span.attributes or {})["http.response.status_code"] == 400
    assert [event for event in span.events if event.name == "exception"] == []


def test_on_startup_activates_and_emits_startup_event(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    app = make_app(monkeypatch, app_version="1.2.3")
    assert not activation.is_activated()
    with TestClient(app=app):
        # Activation completed during lifespan startup, before any request
        assert activation.is_activated()

    payload = startup_payload(exporters)
    assert payload["framework"] == "litestar"
    assert payload["versions"] == {
        "python": platform.python_version(),
        "litestar": metadata.version("litestar"),
        "app": "1.2.3",
    }
    assert sorted(payload["paths"], key=lambda p: (p["path"], p["method"])) == [
        {"method": "GET", "path": "/healthz"},
        {"method": "POST", "path": "/users"},
        {"method": "GET", "path": "/users/{user_id}"},
    ]
    assert "/users/{user_id}" in json.loads(payload["openapi"])["paths"]


def test_buffered_telemetry_flushed_on_lifespan_shutdown(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    with TestClient(app=make_app(monkeypatch)) as client:
        assert client.get("/users/123").status_code == 200

    worker = activation.export_worker
    assert worker is not None and worker.stop_event.is_set()
    assert exporters.span[0].get_finished_spans()


def test_plugin_reconstruction_same_kwargs_is_noop(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    make_app(monkeypatch, env="dev")
    first_config = config.get_config()
    app = make_app(monkeypatch, env="dev")
    assert config.get_config() is first_config

    with TestClient(app=app) as client:
        assert client.get("/users/123").status_code == 200
    (span,) = exported_spans(exporters)
    assert span.kind == SpanKind.SERVER
