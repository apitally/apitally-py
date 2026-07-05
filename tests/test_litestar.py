from __future__ import annotations

import json
import platform
from importlib import metadata
from typing import Any

import pytest
from litestar import Litestar, get, post
from litestar.middleware.base import DefineMiddleware
from litestar.plugins.opentelemetry import (
    OpenTelemetryConfig,
    OpenTelemetryInstrumentationMiddleware,
    OpenTelemetryPlugin,
)
from litestar.testing import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind, StatusCode

from apitally.litestar import ApitallyPlugin
from apitally.shared import activation, config, metrics, startup
from apitally.shared.redaction import REDACTED
from tests.conftest import CreatedExporters


TOKEN = "apt_" + "a" * 24


@get("/users/{user_id:int}")
async def get_user(user_id: int) -> dict[str, int]:
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


ROUTE_HANDLERS = [get_user, create_user, healthz]


def make_app(plugins: list[Any] | None = None, middleware: list[Any] | None = None, **plugin_kwargs: Any) -> Litestar:
    return Litestar(
        route_handlers=ROUTE_HANDLERS,
        plugins=[*(plugins or []), ApitallyPlugin(write_token=TOKEN, **plugin_kwargs)],
        middleware=middleware or [],
    )


def flush_spans(memory_exporters: CreatedExporters) -> list[ReadableSpan]:
    assert activation.span_processor is not None
    activation.span_processor.force_flush()
    return [span for exporter in memory_exporters.span for span in exporter.get_finished_spans()]


def test_route_repair_metrics_and_no_noise_spans(memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with TestClient(app=make_app()) as client:
        assert metrics.meter_provider is not None
        reader = InMemoryMetricReader(**metrics.HISTOGRAM_OVERRIDES)
        metrics.meter_provider.add_metric_reader(reader)
        assert client.get("/users/123").status_code == 200

    (span,) = flush_spans(memory_exporters)
    assert span.kind == SpanKind.SERVER
    assert span.name == "GET /users/{user_id}"
    assert (span.attributes or {})["http.route"] == "/users/{user_id}"

    metrics_data = reader.get_metrics_data()
    assert metrics_data is not None
    points = [
        dict(point.attributes or {})
        for resource_metrics in metrics_data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == "http.server.request.duration"
        for point in metric.data.data_points
    ]
    assert len(points) == 1
    assert points[0]["http.route"] == "/users/{user_id}"


def test_user_otel_plugin_detected_and_repaired(memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    app = make_app(plugins=[OpenTelemetryPlugin(OpenTelemetryConfig())])
    assert sum(isinstance(plugin, OpenTelemetryPlugin) for plugin in app.plugins.init) == 1
    with TestClient(app=app) as client:
        assert client.get("/users/123").status_code == 200

    # The user's config has no exclude_spans; the backstop drops the receive/send spans
    (span,) = flush_spans(memory_exporters)
    assert span.kind == SpanKind.SERVER
    assert span.name == "GET /users/{user_id}"
    assert (span.attributes or {})["http.route"] == "/users/{user_id}"


def test_legacy_otel_middleware_detected_and_repaired(
    memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    legacy = DefineMiddleware(OpenTelemetryInstrumentationMiddleware, config=OpenTelemetryConfig())
    app = make_app(middleware=[legacy])
    assert sum(isinstance(plugin, OpenTelemetryPlugin) for plugin in app.plugins.init) == 1
    with TestClient(app=app) as client:
        assert client.get("/users/123").status_code == 200

    (span,) = flush_spans(memory_exporters)
    assert span.kind == SpanKind.SERVER
    assert span.name == "GET /users/{user_id}"


def test_excluded_request_exports_nothing(memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with TestClient(app=make_app()) as client:
        assert client.get("/healthz").status_code == 200

    assert flush_spans(memory_exporters) == []


def test_body_capture_on_server_span(memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with TestClient(app=make_app(log_request_body=True, log_response_body=True)) as client:
        response = client.post("/users", json={"user": "u", "password": "secret"})
        assert response.status_code == 201

    (span,) = flush_spans(memory_exporters)
    attributes = span.attributes or {}
    assert json.loads(str(attributes["apitally.request.body"])) == {"user": "u", "password": REDACTED}
    assert json.loads(str(attributes["apitally.response.body"])) == {"user": "u", "password": REDACTED}


def test_unhandled_exception_recorded_on_server_span(
    memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    app = Litestar(route_handlers=[error_route], plugins=[ApitallyPlugin(write_token=TOKEN)])
    with TestClient(app=app) as client:
        assert client.get("/error").status_code == 500

    (span,) = flush_spans(memory_exporters)
    assert span.kind == SpanKind.SERVER
    assert span.status.status_code == StatusCode.ERROR
    assert (span.attributes or {})["http.response.status_code"] == 500
    (event,) = [event for event in span.events if event.name == "exception"]
    assert (event.attributes or {})["exception.type"] == "ValueError"
    assert (event.attributes or {})["exception.message"] == "boom"


def test_on_startup_activates_and_emits_startup_event(
    memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    app = make_app(app_version="1.2.3")
    assert not activation.is_activated()
    with TestClient(app=app):
        # Activation completed during lifespan startup, before any request
        assert activation.is_activated()

    assert activation.log_processor is not None
    activation.log_processor.force_flush()
    (exported,) = [
        record
        for exporter in memory_exporters.log
        for record in exporter.get_finished_logs()
        if record.log_record.event_name == startup.EVENT_NAME
    ]
    assert isinstance(exported.log_record.body, str)
    payload = json.loads(exported.log_record.body)
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


def test_plugin_reconstruction_same_kwargs_is_noop(memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    make_app(env="dev")
    first_config = config.get_config()
    app = make_app(env="dev")
    assert config.get_config() is first_config

    with TestClient(app=app) as client:
        assert client.get("/users/123").status_code == 200
    (span,) = flush_spans(memory_exporters)
    assert span.kind == SpanKind.SERVER
