import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import SpanKind

from apitally import set_consumer
from apitally.fastapi import init_apitally
from apitally.shared import activation, startup
from apitally.shared.asgi import ApitallyASGIMiddleware
from tests.conftest import (
    WRITE_TOKEN,
    InMemoryExporters,
    attach_metric_reader,
    duration_data_points,
    exported_log_records,
    exported_spans,
    startup_payload,
    unwrap,
)


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


def init(app: FastAPI, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    init_apitally(app, write_token=WRITE_TOKEN, **kwargs)


def test_request_exports_single_server_span_with_stable_semconv(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)
    with TestClient(app) as client:
        client.get("/items/42")
    (span,) = exported_spans(exporters)  # exactly one: no receive/send spans
    assert span.kind == SpanKind.SERVER
    assert unwrap(span.attributes)["http.request.method"] == "GET"
    assert unwrap(span.attributes)["http.route"] == "/items/{item_id}"
    assert unwrap(span.attributes)["http.response.status_code"] == 200


def test_histogram_attributes_and_log_correlation(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)
    with TestClient(app) as client:
        reader = attach_metric_reader()
        client.get("/items/42")
        client.get("/v1/users/7")

    spans = {unwrap(span.attributes)["http.route"]: span for span in exported_spans(exporters)}
    points = {unwrap(point.attributes)["http.route"]: point for point in duration_data_points(reader)}
    assert points["/items/{item_id}"].attributes == {
        "http.request.method": "GET",
        "http.route": "/items/{item_id}",
        "http.response.status_code": 200,
        "url.scheme": "http",
    }
    # Included-router route resolves to the full template, matching the SERVER span
    assert set(points) == set(spans) == {"/items/{item_id}", "/v1/users/{user_id}"}

    (record,) = [r for r in exported_log_records(exporters) if r.body == "handling item 42"]
    server_span_id = format(spans["/items/{item_id}"].context.span_id, "016x")
    assert unwrap(record.attributes)["apitally.request.server_span_id"] == server_span_id


def test_lifespan_activates_before_first_request_and_startup_event_first(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)
    assert not activation.is_activated()
    with TestClient(app) as client:
        assert activation.is_activated()
        client.get("/items/1")
    records = exported_log_records(exporters)
    assert len(records) >= 2
    assert records[0].event_name == startup.EVENT_NAME


def test_healthz_excluded_from_spans_counted_in_metrics_options_in_neither(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)
    with TestClient(app) as client:
        reader = attach_metric_reader()
        client.get("/healthz")
        client.options("/items/42")
    assert exported_spans(exporters) == []
    (point,) = duration_data_points(reader)
    assert unwrap(point.attributes)["http.route"] == "/healthz"


def test_request_body_captured_and_redacted(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch, log_request_body=True)
    with TestClient(app) as client:
        client.post("/items", json={"name": "widget", "password": "hunter2"})
    (span,) = exported_spans(exporters)
    body = unwrap(span.attributes)["apitally.request.body"]
    assert isinstance(body, str)
    assert json.loads(body) == {"name": "widget", "password": "[REDACTED]"}
    body_size = unwrap(span.attributes)["http.request.body.size"]
    assert isinstance(body_size, int) and body_size > 0


def test_mounted_subapp_route_includes_mount_prefix(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    subapp = FastAPI()

    @subapp.get("/things/{thing_id}")
    def get_thing(thing_id: int) -> dict[str, int]:
        return {"thing_id": thing_id}

    app.mount("/sub", subapp)
    init(app, monkeypatch)
    with TestClient(app) as client:
        reader = attach_metric_reader()
        client.get("/sub/things/7")

    (span,) = exported_spans(exporters)
    (point,) = duration_data_points(reader)
    assert span.name == "GET /sub/things/{thing_id}"
    assert unwrap(span.attributes)["http.route"] == "/sub/things/{thing_id}"
    assert unwrap(point.attributes)["http.route"] == "/sub/things/{thing_id}"


def test_pre_instrumented_app_adapts_without_duplicate_spans(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    FastAPIInstrumentor.instrument_app(app)
    init(app, monkeypatch)
    with TestClient(app) as client:
        reader = attach_metric_reader()
        client.get("/items/42")
    # The user instrumentor's receive/send spans are dropped by the span processor's built-in filter
    (span,) = exported_spans(exporters)
    assert span.kind == SpanKind.SERVER
    response_body_size = unwrap(span.attributes)["http.response.body.size"]
    assert isinstance(response_body_size, int) and response_body_size > 0
    (point,) = duration_data_points(reader)
    assert unwrap(point.attributes)["http.route"] == "/items/{item_id}"


def test_unhandled_exception_recorded_on_server_span(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/error")
    assert response.status_code == 500
    (span,) = exported_spans(exporters)
    assert unwrap(span.attributes)["http.response.status_code"] == 500
    (event,) = [e for e in span.events if e.name == "exception"]
    assert unwrap(event.attributes)["exception.type"] == "ValueError"
    assert unwrap(event.attributes)["exception.message"] == "boom"


def test_unhandled_exception_response_captured(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch, log_response_headers=True, log_response_body=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/error")
    assert response.status_code == 500
    (span,) = exported_spans(exporters)
    attributes = unwrap(span.attributes)
    assert attributes["http.response.header.content-type"] == ("text/plain; charset=utf-8",)
    assert attributes["apitally.response.body"] == "Internal Server Error"
    assert attributes["http.response.body.size"] == len("Internal Server Error")


def test_startup_event_paths_match_routes_and_openapi_parses(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch, app_version="1.2.3")
    with TestClient(app):
        pass
    payload = startup_payload(exporters)
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
    # sync endpoints run in a copied context (threadpool); set_consumer must reach metrics
    # through the holder shared by reference across context copies
    init(app, monkeypatch)
    with TestClient(app) as client:
        reader = attach_metric_reader()
        client.get("/consumer")
    (point,) = duration_data_points(reader)
    assert unwrap(point.attributes)["apitally.consumer.identifier"] == "tester"


def test_sample_rate_zero_drops_spans_keeps_metrics(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    # Pins that sampling kwargs passed to init_apitally reach the config, exercised through a real framework
    init(app, monkeypatch, sample_rate=0.0)
    with TestClient(app) as client:
        reader = attach_metric_reader()
        client.get("/items/42")
    assert exported_spans(exporters) == []
    (point,) = duration_data_points(reader)
    assert unwrap(point.attributes)["http.route"] == "/items/{item_id}"


def test_init_twice_does_not_stack_middleware(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)
    init(app, monkeypatch)
    layer = app.build_middleware_stack()
    count = 0
    while layer is not None:
        count += isinstance(layer, ApitallyASGIMiddleware)
        layer = getattr(layer, "app", None)
    assert count == 1
    with TestClient(app) as client:
        client.get("/items/1")
    assert len(exported_spans(exporters)) == 1


def test_init_without_write_token_exports_nothing(
    app: FastAPI, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("APITALLY_WRITE_TOKEN", raising=False)
    init_apitally(app)
    with TestClient(app) as client:
        assert client.get("/items/42").status_code == 200
    assert not activation.is_activated()
    assert exporters.span == exporters.log == exporters.metric == []
