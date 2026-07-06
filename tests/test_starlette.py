import json
import logging
from typing import Any, Iterator, cast

import pytest
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.starlette import StarletteInstrumentor
from opentelemetry.trace import SpanKind
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from apitally.shared import activation, startup
from apitally.shared.asgi import ApitallyASGIMiddleware
from apitally.starlette import init_apitally
from tests.conftest import (
    WRITE_TOKEN,
    InMemoryExporters,
    attach_metric_reader,
    duration_data_points,
    exported_log_records,
    exported_spans,
    unwrap,
)


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


def init(app: Starlette, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    init_apitally(app, write_token=WRITE_TOKEN, **kwargs)


def test_request_flow_span_histogram_and_startup_event(
    app: Starlette, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)
    assert not activation.is_activated()
    with TestClient(app) as client:
        assert activation.is_activated()
        reader = attach_metric_reader()
        client.get("/items/42")

    # Exactly one exported span: the Starlette instrumentor emits receive/send spans
    # (no exclude_spans support) and the span processor backstop drops them
    (span,) = exported_spans(exporters)
    assert span.kind == SpanKind.SERVER
    assert unwrap(span.attributes)["http.request.method"] == "GET"
    assert unwrap(span.attributes)["http.route"] == "/items/{item_id}"
    assert unwrap(span.attributes)["http.response.status_code"] == 200

    (point,) = duration_data_points(reader)
    assert unwrap(point.attributes)["http.route"] == "/items/{item_id}"
    assert unwrap(point.attributes)["http.request.method"] == "GET"

    records = exported_log_records(exporters)
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
    init_apitally(app, write_token=WRITE_TOKEN)
    with TestClient(app) as client:
        response = client.get("/items/1")
    assert response.status_code == 200


def test_pre_instrumented_app_inserts_transport_inside_otel_middleware(
    app: Starlette, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    StarletteInstrumentor.instrument_app(app)
    init(app, monkeypatch)

    classes = [cast(type, m.cls) for m in app.user_middleware]
    assert classes.index(activation.ASGIActivationShim) == 0
    assert classes.index(ApitallyASGIMiddleware) == classes.index(OpenTelemetryMiddleware) + 1

    with TestClient(app) as client:
        reader = attach_metric_reader()
        client.get("/items/42")
    (span,) = exported_spans(exporters)
    assert span.kind == SpanKind.SERVER
    response_body_size = unwrap(span.attributes)["http.response.body.size"]
    assert isinstance(response_body_size, int) and response_body_size > 0
    (point,) = duration_data_points(reader)
    assert unwrap(point.attributes)["http.route"] == "/items/{item_id}"
