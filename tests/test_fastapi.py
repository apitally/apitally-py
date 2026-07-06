import json
import logging
from collections.abc import Iterator
from typing import Any, NoReturn

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from opentelemetry._logs import LogRecord
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics.export import DataPointT, InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind

from apitally import set_consumer
from apitally.fastapi import init_apitally
from apitally.shared import activation, metrics, startup
from apitally.shared.asgi import ApitallyASGIMiddleware
from tests.conftest import InMemoryExporters, unwrap


TOKEN = "apt_" + "a" * 24


def create_app() -> FastAPI:
    app = FastAPI()

    @app.get("/items/{item_id}", summary="Get item")
    def get_item(item_id: int) -> dict[str, int]:
        logging.getLogger("myapp").warning("handling item %s", item_id)
        return {"item_id": item_id}

    @app.post("/items")
    def create_item(data: dict[str, Any]) -> dict[str, Any]:
        return data

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/error")
    def error() -> None:
        raise ValueError("boom")

    @app.get("/consumer")
    def consumer() -> dict[str, bool]:
        set_consumer("tester")
        return {"ok": True}

    router = APIRouter()

    @router.get("/users/{user_id}")
    def get_user(user_id: int) -> dict[str, int]:
        return {"user_id": user_id}

    app.include_router(router, prefix="/v1")
    return app


@pytest.fixture()
def app() -> Iterator[FastAPI]:
    app = create_app()
    yield app
    FastAPIInstrumentor.uninstrument_app(app)


def get_finished_spans(exporters: InMemoryExporters) -> list[ReadableSpan]:
    assert activation.span_processor is not None
    activation.span_processor.force_flush()
    return [span for exporter in exporters.span for span in exporter.get_finished_spans()]


def get_log_records(exporters: InMemoryExporters) -> list[LogRecord]:
    assert activation.log_processor is not None
    activation.log_processor.force_flush()
    return [exported.log_record for exporter in exporters.log for exported in exporter.get_finished_logs()]


def attach_metric_reader() -> InMemoryMetricReader:
    assert metrics.meter_provider is not None
    reader = InMemoryMetricReader(**metrics.HISTOGRAM_OVERRIDES)
    metrics.meter_provider.add_metric_reader(reader)
    return reader


def duration_points(reader: InMemoryMetricReader) -> list[DataPointT]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        point
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == "http.server.request.duration"
        for point in metric.data.data_points
    ]


def test_request_exports_single_server_span_with_stable_semconv(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    init_apitally(app, write_token=TOKEN)
    with TestClient(app) as client:
        client.get("/items/42")
    (span,) = get_finished_spans(exporters)  # exactly one: no receive/send spans
    assert span.kind == SpanKind.SERVER
    assert unwrap(span.attributes)["http.request.method"] == "GET"
    assert unwrap(span.attributes)["http.route"] == "/items/{item_id}"
    assert unwrap(span.attributes)["http.response.status_code"] == 200


def test_histogram_attributes_and_log_correlation(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    init_apitally(app, write_token=TOKEN)
    with TestClient(app) as client:
        reader = attach_metric_reader()
        client.get("/items/42")
        client.get("/v1/users/7")

    spans = {unwrap(span.attributes)["http.route"]: span for span in get_finished_spans(exporters)}
    points = {unwrap(point.attributes)["http.route"]: point for point in duration_points(reader)}
    assert points["/items/{item_id}"].attributes == {
        "http.request.method": "GET",
        "http.route": "/items/{item_id}",
        "http.response.status_code": 200,
        "url.scheme": "http",
    }
    # Included-router route resolves to the full template, matching the SERVER span
    assert set(points) == set(spans) == {"/items/{item_id}", "/v1/users/{user_id}"}

    (record,) = [r for r in get_log_records(exporters) if r.body == "handling item 42"]
    server_span_id = format(spans["/items/{item_id}"].context.span_id, "016x")
    assert unwrap(record.attributes)["apitally.request.server_span_id"] == server_span_id


def test_lifespan_activates_before_first_request_and_startup_event_first(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    init_apitally(app, write_token=TOKEN)
    assert not activation.is_activated()
    with TestClient(app) as client:
        assert activation.is_activated()
        client.get("/items/1")
    records = get_log_records(exporters)
    assert len(records) >= 2
    assert records[0].event_name == startup.EVENT_NAME


def test_healthz_excluded_from_spans_counted_in_metrics_options_in_neither(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    init_apitally(app, write_token=TOKEN)
    with TestClient(app) as client:
        reader = attach_metric_reader()
        client.get("/healthz")
        client.options("/items/42")
    assert get_finished_spans(exporters) == []
    (point,) = duration_points(reader)
    assert unwrap(point.attributes)["http.route"] == "/healthz"


def test_request_body_captured_and_redacted(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    init_apitally(app, write_token=TOKEN, log_request_body=True)
    with TestClient(app) as client:
        client.post("/items", json={"name": "widget", "password": "hunter2"})
    (span,) = get_finished_spans(exporters)
    body = unwrap(span.attributes)["apitally.request.body"]
    assert isinstance(body, str)
    assert json.loads(body) == {"name": "widget", "password": "[REDACTED]"}
    body_size = unwrap(span.attributes)["http.request.body.size"]
    assert isinstance(body_size, int) and body_size > 0


def test_pre_instrumented_app_adapts_without_duplicate_spans(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    FastAPIInstrumentor.instrument_app(app)
    init_apitally(app, write_token=TOKEN)
    with TestClient(app) as client:
        reader = attach_metric_reader()
        client.get("/items/42")
    # One SERVER span, and the receive/send spans from the user's plain instrument_app
    # call are dropped by the span processor backstop
    (span,) = get_finished_spans(exporters)
    assert span.kind == SpanKind.SERVER
    response_body_size = unwrap(span.attributes)["http.response.body.size"]
    assert isinstance(response_body_size, int) and response_body_size > 0
    (point,) = duration_points(reader)
    assert unwrap(point.attributes)["http.route"] == "/items/{item_id}"


def test_unhandled_exception_recorded_as_event_on_500_span(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    init_apitally(app, write_token=TOKEN)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/error")
    assert response.status_code == 500
    (span,) = get_finished_spans(exporters)
    assert unwrap(span.attributes)["http.response.status_code"] == 500
    (event,) = [e for e in span.events if e.name == "exception"]
    assert unwrap(event.attributes)["exception.type"] == "ValueError"
    assert unwrap(event.attributes)["exception.message"] == "boom"


def test_startup_event_paths_match_routes_and_openapi_parses(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    init_apitally(app, write_token=TOKEN, app_version="1.2.3")
    with TestClient(app):
        pass
    (record,) = [r for r in get_log_records(exporters) if r.event_name == startup.EVENT_NAME]
    assert isinstance(record.body, str)
    payload = json.loads(record.body)
    assert payload["framework"] == "fastapi"
    assert "fastapi" in payload["versions"]
    assert payload["versions"]["app"] == "1.2.3"
    paths = payload["paths"]
    assert {"method": "GET", "path": "/items/{item_id}", "summary": "Get item"} in paths
    assert {"method": "POST", "path": "/items"} in paths
    assert {"method": "GET", "path": "/v1/users/{user_id}"} in paths
    assert not any(entry["path"] == "/openapi.json" for entry in paths)
    openapi = json.loads(payload["openapi"])
    assert openapi["openapi"].startswith("3.")
    assert "/items/{item_id}" in openapi["paths"]


def test_consumer_set_in_sync_endpoint_reaches_metrics(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    # def endpoints run in a copied context (threadpool), so the ContextVar write is lost
    # and the span attribute fallback must carry the consumer into the histogram
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    init_apitally(app, write_token=TOKEN)
    with TestClient(app) as client:
        reader = attach_metric_reader()
        client.get("/consumer")
    (point,) = duration_points(reader)
    assert unwrap(point.attributes)["apitally.consumer.identifier"] == "tester"


def test_sample_rate_zero_drops_spans_keeps_metrics(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    # Pins the adapter-to-config plumbing for the sampling kwargs through a real framework
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    init_apitally(app, write_token=TOKEN, sample_rate=0.0)
    with TestClient(app) as client:
        reader = attach_metric_reader()
        client.get("/items/42")
    assert get_finished_spans(exporters) == []
    (point,) = duration_points(reader)
    assert unwrap(point.attributes)["http.route"] == "/items/{item_id}"


def test_init_apitally_swallows_instrumentation_errors(app: FastAPI, monkeypatch: pytest.MonkeyPatch):
    def raise_error(*args: Any, **kwargs: Any) -> NoReturn:
        raise RuntimeError("instrumentation failed")

    monkeypatch.setattr(FastAPIInstrumentor, "instrument_app", raise_error)
    init_apitally(app, write_token=TOKEN)
    with TestClient(app) as client:
        response = client.get("/items/1")
    assert response.status_code == 200


def test_init_twice_does_not_stack_middleware(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    init_apitally(app, write_token=TOKEN)
    init_apitally(app, write_token=TOKEN)
    assert sum(1 for m in app.user_middleware if m.cls is ApitallyASGIMiddleware) == 1
    with TestClient(app) as client:
        client.get("/items/1")
    assert len(get_finished_spans(exporters)) == 1
