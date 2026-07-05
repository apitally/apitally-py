import json
import logging
from typing import Any, Iterator, cast

import pytest
from opentelemetry._logs import LogRecord
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.starlette import StarletteInstrumentor
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from apitally.shared import activation, metrics, startup
from apitally.shared.asgi import ApitallyASGIMiddleware
from apitally.starlette import init_apitally
from tests.conftest import CreatedExporters, unwrap


TOKEN = "apt_" + "a" * 24


def create_app() -> Starlette:
    async def get_item(request: Request) -> JSONResponse:
        logging.getLogger("myapp").warning("handling item")
        return JSONResponse({"item_id": request.path_params["item_id"]})

    return Starlette(routes=[Route("/items/{item_id}", get_item)])


@pytest.fixture()
def app() -> Iterator[Starlette]:
    app = create_app()
    yield app
    StarletteInstrumentor.uninstrument_app(app)


def get_finished_spans(memory_exporters: CreatedExporters) -> list[ReadableSpan]:
    assert activation.span_processor is not None
    activation.span_processor.force_flush()
    return [span for exporter in memory_exporters.span for span in exporter.get_finished_spans()]


def get_log_records(memory_exporters: CreatedExporters) -> list[LogRecord]:
    assert activation.log_processor is not None
    activation.log_processor.force_flush()
    return [exported.log_record for exporter in memory_exporters.log for exported in exporter.get_finished_logs()]


def attach_metric_reader() -> InMemoryMetricReader:
    assert metrics.meter_provider is not None
    reader = InMemoryMetricReader(**metrics.HISTOGRAM_OVERRIDES)
    metrics.meter_provider.add_metric_reader(reader)
    return reader


def duration_points(reader: InMemoryMetricReader) -> list[Any]:
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


def test_request_flow_span_histogram_and_startup_event(
    app: Starlette, memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    init_apitally(app, write_token=TOKEN)
    assert not activation.is_activated()
    with TestClient(app) as client:
        assert activation.is_activated()
        reader = attach_metric_reader()
        client.get("/items/42")

    # Exactly one exported span: the Starlette instrumentor emits receive/send spans
    # (no exclude_spans support) and the span processor backstop drops them
    (span,) = get_finished_spans(memory_exporters)
    assert span.kind == SpanKind.SERVER
    assert unwrap(span.attributes)["http.request.method"] == "GET"
    assert unwrap(span.attributes)["http.route"] == "/items/{item_id}"
    assert unwrap(span.attributes)["http.response.status_code"] == 200

    (point,) = duration_points(reader)
    assert unwrap(point.attributes)["http.route"] == "/items/{item_id}"
    assert unwrap(point.attributes)["http.request.method"] == "GET"

    records = get_log_records(memory_exporters)
    assert records[0].event_name == startup.EVENT_NAME
    assert isinstance(records[0].body, str)
    payload = json.loads(records[0].body)
    assert payload["framework"] == "starlette"
    assert "starlette" in payload["versions"]
    assert {"method": "get", "path": "/items/{item_id}"} in payload["paths"]
    (record,) = [r for r in records if r.body == "handling item"]
    assert unwrap(record.attributes)["apitally.request.server_span_id"] == format(span.context.span_id, "016x")


def test_init_apitally_swallows_instrumentation_errors(app: Starlette, monkeypatch: pytest.MonkeyPatch):
    def raise_error(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("instrumentation failed")

    monkeypatch.setattr(StarletteInstrumentor, "instrument_app", raise_error)
    init_apitally(app, write_token=TOKEN)
    with TestClient(app) as client:
        response = client.get("/items/1")
    assert response.status_code == 200


def test_pre_instrumented_app_inserts_transport_inside_otel_middleware(
    app: Starlette, memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    StarletteInstrumentor.instrument_app(app)
    init_apitally(app, write_token=TOKEN)

    classes = [cast(type, m.cls) for m in app.user_middleware]
    assert classes.index(activation.ASGIActivationShim) == 0
    assert classes.index(ApitallyASGIMiddleware) == classes.index(OpenTelemetryMiddleware) + 1

    with TestClient(app) as client:
        reader = attach_metric_reader()
        client.get("/items/42")
    (span,) = get_finished_spans(memory_exporters)
    assert span.kind == SpanKind.SERVER
    response_body_size = unwrap(span.attributes)["http.response.body.size"]
    assert isinstance(response_body_size, int) and response_body_size > 0
    (point,) = duration_points(reader)
    assert unwrap(point.attributes)["http.route"] == "/items/{item_id}"
