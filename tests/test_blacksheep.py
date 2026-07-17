import inspect
import json
from importlib.metadata import version
from typing import Any

import httpx
import pytest
from blacksheep import Application, Request, Response, text
from blacksheep.server.routing import Router
from opentelemetry import context as otel_context
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.trace import SpanKind, StatusCode

import apitally
from apitally.shared import activation
from apitally.shared.redaction import REDACTED
from tests.conftest import (
    WRITE_TOKEN,
    InMemoryExporters,
    attach_metric_reader,
    attach_stale_server_span,
    duration_data_points,
    exported_spans,
    startup_payload,
)


def allow_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    # pytest re-sets PYTEST_CURRENT_TEST per test phase, so the activation guard must be
    # disabled inside the test body, not in a fixture
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)


def create_app(**kwargs: Any) -> Application:
    app = Application(router=Router())
    apitally.init(app, write_token=WRITE_TOKEN, app_version="1.2.3", **kwargs)

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


async def test_request_exports_span_with_route_and_records_metrics(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    allow_activation(monkeypatch)
    app = create_app()
    await app.start()
    assert activation.is_activated()
    reader = attach_metric_reader()

    async with create_client(app) as client:
        response = await client.get("/items/123")
    assert response.status_code == 200

    (span,) = exported_spans(exporters, kind=SpanKind.SERVER)
    assert span.name == "GET /items/{id}"
    assert span.attributes is not None
    assert span.attributes["http.route"] == "/items/{id}"

    (point,) = duration_data_points(reader)
    attributes = dict(point.attributes or {})
    assert attributes["http.route"] == "/items/{id}"
    assert attributes["http.request.method"] == "GET"
    assert attributes["http.response.status_code"] == 200

    payload = startup_payload(exporters)
    assert payload["framework"] == "blacksheep"
    assert payload["versions"]["blacksheep"] == version("blacksheep")
    assert payload["versions"]["app"] == "1.2.3"
    assert {"method": "GET", "path": "/items/{id}"} in payload["paths"]
    assert {"method": "POST", "path": "/items"} in payload["paths"]
    assert "openapi" not in payload


async def test_startup_paths_include_sub_router_routes(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    allow_activation(monkeypatch)
    books = Router(prefix="/books")

    @books.get("/{book_id}")
    def get_book(book_id: str) -> Response:
        return text(f"book {book_id}")

    app = Application(router=Router(sub_routers=[books]))
    apitally.init(app, write_token=WRITE_TOKEN)

    @app.router.get("/health")
    def health() -> Response:
        return text("ok")

    await app.start()

    payload = startup_payload(exporters)
    assert {"method": "GET", "path": "/books/{book_id}"} in payload["paths"]
    assert {"method": "GET", "path": "/health"} in payload["paths"]


async def test_unmatched_request_has_no_route_and_no_histogram_point(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    allow_activation(monkeypatch)
    app = create_app()
    await app.start()
    reader = attach_metric_reader()

    async with create_client(app) as client:
        response = await client.get("/nope")
    assert response.status_code == 404

    (span,) = exported_spans(exporters, kind=SpanKind.SERVER)
    assert span.attributes is not None
    assert "http.route" not in span.attributes
    assert duration_data_points(reader) == []


async def test_first_request_activates_and_records_without_lifespan(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    # init wraps app._handle_http, the one private-API dependency; this assertion
    # fails loudly if BlackSheep renames it or changes its signature
    assert list(inspect.signature(Application._handle_http).parameters) == ["self", "scope", "receive", "send"]

    allow_activation(monkeypatch)
    app = create_app()
    assert isinstance(app.__dict__["_handle_http"], activation.ASGIActivationShim)
    assert not activation.is_activated()

    async with create_client(app) as client:
        response = await client.get("/items/123")
    assert response.status_code == 200
    assert activation.is_activated()

    (span,) = exported_spans(exporters, kind=SpanKind.SERVER)
    assert span.name == "GET /items/{id}"


async def test_buffered_telemetry_flushed_on_lifespan_shutdown(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    allow_activation(monkeypatch)
    app = create_app()
    await app.start()

    async with create_client(app) as client:
        assert (await client.get("/items/123")).status_code == 200
    await app.stop()

    worker = activation.export_worker
    assert worker is not None and worker.stop_event.is_set()
    assert exporters.span[0].get_finished_spans()


async def test_pre_instrumented_app_adapts_without_duplicate_spans(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    allow_activation(monkeypatch)
    app = Application(router=Router())
    wrapped = OpenTelemetryMiddleware(app)
    apitally.init(wrapped, write_token=WRITE_TOKEN)

    @app.router.get("/items/{id}")
    def get_item(id: str) -> Response:
        return text(f"item {id}")

    await app.start()

    async with create_client(wrapped) as client:
        response = await client.get("/items/1")
    assert response.status_code == 200

    spans = exported_spans(exporters)
    (server_span,) = [s for s in spans if s.kind == SpanKind.SERVER]
    assert server_span.name == "GET /items/{id}"
    assert server_span.attributes is not None
    assert server_span.attributes["http.route"] == "/items/{id}"
    # The user's instrumentor emits receive/send INTERNAL spans; the span processor drops them
    assert [s for s in spans if s.kind == SpanKind.INTERNAL] == []


async def test_unhandled_exception_recorded_on_server_span(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    allow_activation(monkeypatch)
    app = create_app()

    @app.router.get("/error")
    def error_route() -> Response:
        raise ValueError("boom")

    await app.start()

    async with create_client(app) as client:
        response = await client.get("/error")
    assert response.status_code == 500

    (span,) = exported_spans(exporters, kind=SpanKind.SERVER)
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes is not None
    assert span.attributes["http.response.status_code"] == 500
    (event,) = [event for event in span.events if event.name == "exception"]
    assert (event.attributes or {})["exception.type"] == "ValueError"
    assert (event.attributes or {})["exception.message"] == "boom"


async def test_init_twice_does_not_stack_middleware(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    allow_activation(monkeypatch)
    app = create_app()
    handler = app.__dict__["_handle_http"]
    apitally.init(app, write_token=WRITE_TOKEN, app_version="1.2.3")
    assert app.__dict__["_handle_http"] is handler

    await app.start()
    async with create_client(app) as client:
        response = await client.get("/items/1")
    assert response.status_code == 200
    (span,) = exported_spans(exporters, kind=SpanKind.SERVER)
    assert span.name == "GET /items/{id}"


async def test_request_body_captured_and_redacted(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    allow_activation(monkeypatch)
    app = create_app(capture_request_body=True)
    await app.start()

    async with create_client(app) as client:
        response = await client.post("/items", json={"password": "secret", "name": "widget"})
    assert response.status_code == 200

    (span,) = exported_spans(exporters, kind=SpanKind.SERVER)
    assert span.attributes is not None
    assert json.loads(str(span.attributes["apitally.request.body"])) == {"password": REDACTED, "name": "widget"}


async def test_request_with_leaked_context_still_exports_server_span(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    allow_activation(monkeypatch)
    app = create_app()
    await app.start()
    _, token = attach_stale_server_span()
    try:
        async with create_client(app) as client:
            assert (await client.get("/items/123")).status_code == 200
    finally:
        otel_context.detach(token)

    (span,) = exported_spans(exporters, kind=SpanKind.SERVER)
    assert span.parent is None
