from __future__ import annotations

import inspect
import json
from importlib.metadata import version
from typing import Any

import httpx
import pytest
from blacksheep import Application, Request, Response, text
from blacksheep.server.routing import Router
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.sdk.metrics.export import ExponentialHistogram, InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind

from apitally.blacksheep import init_apitally
from apitally.shared import activation, metrics, startup
from apitally.shared.redaction import REDACTED


TOKEN = "apt_" + "a" * 24


def allow_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    # pytest re-sets PYTEST_CURRENT_TEST per test phase, so the activation guard must be
    # disabled inside the test body, not in a fixture
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)


def create_app(**kwargs: Any) -> Application:
    app = Application(router=Router())
    init_apitally(app, write_token=TOKEN, app_version="1.2.3", **kwargs)

    @app.router.get("/items/{id}")
    def get_item(id: str) -> Response:
        return text(f"item {id}")

    @app.router.post("/items")
    async def create_item(request: Request) -> Response:
        await request.json()
        return text("created")

    return app


def create_client(asgi_app: Any) -> httpx.AsyncClient:
    # BlackSheep's TestClient calls app.handle() directly, bypassing __call__/_handle_http,
    # so requests are driven through the real ASGI path instead
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app), base_url="http://testserver")


def exported_spans(memory_exporters) -> list[ReadableSpan]:
    assert activation.span_processor is not None
    activation.span_processor.force_flush()
    return [span for exporter in memory_exporters.span for span in exporter.get_finished_spans()]


def attach_metric_reader() -> InMemoryMetricReader:
    assert metrics.meter_provider is not None
    reader = InMemoryMetricReader(**metrics.HISTOGRAM_OVERRIDES)
    metrics.meter_provider.add_metric_reader(reader)
    return reader


def duration_data_points(reader: InMemoryMetricReader) -> list[Any]:
    metrics_data = reader.get_metrics_data()
    if metrics_data is None:
        return []
    return [
        point
        for resource_metrics in metrics_data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == "http.server.request.duration" and isinstance(metric.data, ExponentialHistogram)
        for point in metric.data.data_points
    ]


async def test_request_exports_span_with_route_and_records_metrics(memory_exporters, monkeypatch):
    allow_activation(monkeypatch)
    app = create_app()
    await app.start()
    assert activation.is_activated()
    reader = attach_metric_reader()

    async with create_client(app) as client:
        response = await client.get("/items/123")
    assert response.status_code == 200

    (span,) = [s for s in exported_spans(memory_exporters) if s.kind == SpanKind.SERVER]
    assert span.name == "GET /items/{id}"
    assert span.attributes is not None
    assert span.attributes["http.route"] == "/items/{id}"

    (point,) = duration_data_points(reader)
    attributes = dict(point.attributes or {})
    assert attributes["http.route"] == "/items/{id}"
    assert attributes["http.request.method"] == "GET"
    assert attributes["http.response.status_code"] == 200

    assert activation.log_processor is not None
    activation.log_processor.force_flush()
    (record,) = [
        exported.log_record
        for exporter in memory_exporters.log
        for exported in exporter.get_finished_logs()
        if exported.log_record.event_name == startup.EVENT_NAME
    ]
    payload = json.loads(record.body)
    assert payload["framework"] == "blacksheep"
    assert payload["versions"]["blacksheep"] == version("blacksheep")
    assert payload["versions"]["app"] == "1.2.3"
    assert {"method": "GET", "path": "/items/{id}"} in payload["paths"]
    assert {"method": "POST", "path": "/items"} in payload["paths"]
    assert "openapi" not in payload


async def test_unmatched_request_has_no_route_and_no_histogram_point(memory_exporters, monkeypatch):
    allow_activation(monkeypatch)
    app = create_app()
    await app.start()
    reader = attach_metric_reader()

    async with create_client(app) as client:
        response = await client.get("/nope")
    assert response.status_code == 404

    (span,) = [s for s in exported_spans(memory_exporters) if s.kind == SpanKind.SERVER]
    assert span.attributes is not None
    assert "http.route" not in span.attributes
    assert duration_data_points(reader) == []


async def test_first_request_activates_and_records_without_lifespan(memory_exporters, monkeypatch):
    # PRIVATE-API CANARY (design.md section 4): init_apitally wraps app._handle_http, the one
    # private-API dependency. This test drives the app without lifespan so the request flows
    # through __call__ -> _handle_http; it fails loudly if BlackSheep renames or re-signatures
    # _handle_http, or stops awaiting start() before dispatch.
    assert list(inspect.signature(Application._handle_http).parameters) == ["self", "scope", "receive", "send"]

    allow_activation(monkeypatch)
    app = create_app()
    assert isinstance(app.__dict__["_handle_http"], activation.ASGIActivationShim)
    assert not activation.is_activated()

    async with create_client(app) as client:
        response = await client.get("/items/123")
    assert response.status_code == 200
    assert activation.is_activated()

    (span,) = [s for s in exported_spans(memory_exporters) if s.kind == SpanKind.SERVER]
    assert span.name == "GET /items/{id}"


async def test_preinstrumented_app_adapted_without_duplicate_server_spans(memory_exporters, monkeypatch):
    allow_activation(monkeypatch)
    app = Application(router=Router())
    wrapped = OpenTelemetryMiddleware(app)
    init_apitally(wrapped, write_token=TOKEN)

    @app.router.get("/items/{id}")
    def get_item(id: str) -> Response:
        return text(f"item {id}")

    await app.start()

    async with create_client(wrapped) as client:
        response = await client.get("/items/1")
    assert response.status_code == 200

    spans = exported_spans(memory_exporters)
    (server_span,) = [s for s in spans if s.kind == SpanKind.SERVER]
    assert server_span.name == "GET /items/{id}"
    assert server_span.attributes is not None
    assert server_span.attributes["http.route"] == "/items/{id}"
    # The user's instrumentor emits receive/send INTERNAL spans; the span processor drops them
    assert [s for s in spans if s.kind == SpanKind.INTERNAL] == []


async def test_request_body_captured_and_redacted(memory_exporters, monkeypatch):
    allow_activation(monkeypatch)
    app = create_app(log_request_body=True)
    await app.start()

    async with create_client(app) as client:
        response = await client.post("/items", json={"password": "secret", "name": "widget"})
    assert response.status_code == 200

    (span,) = [s for s in exported_spans(memory_exporters) if s.kind == SpanKind.SERVER]
    assert span.attributes is not None
    assert json.loads(str(span.attributes["apitally.request.body"])) == {"password": REDACTED, "name": "widget"}
