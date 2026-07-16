import json
import logging
from typing import Any, Iterator, cast

import httpx
import pytest
from opentelemetry import context as otel_context
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.starlette import StarletteInstrumentor
from opentelemetry.trace import SpanKind, StatusCode
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

import apitally
from apitally.shared import activation, startup
from apitally.shared.asgi import ApitallyASGIMiddleware
from apitally.shared.redaction import REDACTED
from tests.conftest import (
    WRITE_TOKEN,
    InMemoryExporters,
    attach_metric_reader,
    attach_stale_server_span,
    duration_data_points,
    exported_log_records,
    exported_spans,
    unwrap,
)


def create_app() -> Starlette:
    async def get_item(request: Request) -> JSONResponse:
        logging.getLogger("myapp").warning("handling item")
        return JSONResponse({"item_id": request.path_params["item_id"]})

    async def list_users(request: Request) -> JSONResponse:
        return JSONResponse([])

    async def create_item(request: Request) -> JSONResponse:
        return JSONResponse(await request.json())

    async def error(request: Request) -> JSONResponse:
        raise ValueError("boom")

    return Starlette(
        routes=[
            Route("/items/{item_id}", get_item),
            Route("/items", create_item, methods=["POST"]),
            Route("/error", error),
            Mount("/admin", routes=[Route("/users", list_users)]),
        ]
    )


@pytest.fixture()
def app() -> Iterator[Starlette]:
    app = create_app()
    yield app
    StarletteInstrumentor.uninstrument_app(app)


def init(app: Starlette, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    apitally.init(app, write_token=WRITE_TOKEN, **kwargs)


def test_request_flow_span_histogram_and_startup_event(
    app: Starlette, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)
    assert not activation.is_activated()
    with TestClient(app) as client:
        assert activation.is_activated()
        reader = attach_metric_reader()
        client.get("/items/42")

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


def test_mounted_route_includes_mount_prefix(
    app: Starlette, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)
    with TestClient(app) as client:
        reader = attach_metric_reader()
        client.get("/admin/users")

    (span,) = exported_spans(exporters)
    (point,) = duration_data_points(reader)
    assert span.name == "GET /admin/users"
    assert unwrap(span.attributes)["http.route"] == "/admin/users"
    assert unwrap(point.attributes)["http.route"] == "/admin/users"


def test_request_body_captured_and_redacted(
    app: Starlette, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch, log_request_body=True)
    with TestClient(app) as client:
        client.post("/items", json={"name": "widget", "password": "hunter2"})
    (span,) = exported_spans(exporters)
    body = unwrap(span.attributes)["apitally.request.body"]
    assert isinstance(body, str)
    assert json.loads(body) == {"name": "widget", "password": REDACTED}
    body_size = unwrap(span.attributes)["http.request.body.size"]
    assert isinstance(body_size, int) and body_size > 0


def test_init_twice_does_not_stack_middleware(
    app: Starlette, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
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
        assert client.get("/items/42").status_code == 200
    (span,) = exported_spans(exporters)
    assert span.kind == SpanKind.SERVER


def test_unhandled_exception_recorded_on_server_span(
    app: Starlette, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as client:
        reader = attach_metric_reader()
        response = client.get("/error")
    assert response.status_code == 500

    (span,) = exported_spans(exporters)
    assert unwrap(span.attributes)["http.response.status_code"] == 500
    assert span.status.status_code == StatusCode.ERROR
    (event,) = [e for e in span.events if e.name == "exception"]
    assert unwrap(event.attributes)["exception.type"] == "ValueError"
    assert unwrap(event.attributes)["exception.message"] == "boom"
    (point,) = duration_data_points(reader)
    assert unwrap(point.attributes)["http.response.status_code"] == 500


def test_unhandled_exception_with_http_middleware_recorded_unwrapped(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    # On old Starlette versions, exceptions raised behind BaseHTTPMiddleware propagate
    # wrapped in a single-leaf ExceptionGroup
    async def passthrough(request: Request, call_next: Any) -> Any:
        return await call_next(request)

    async def error(request: Request) -> JSONResponse:
        raise ValueError("boom")

    app = Starlette(
        routes=[Route("/error", error)],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=passthrough)],
    )
    try:
        init(app, monkeypatch)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/error")
        assert response.status_code == 500
        (span,) = exported_spans(exporters)
        assert span.status.status_code == StatusCode.ERROR
        (event,) = [e for e in span.events if e.name == "exception"]
        assert unwrap(event.attributes)["exception.type"] == "ValueError"
        assert unwrap(event.attributes)["exception.message"] == "boom"
    finally:
        StarletteInstrumentor.uninstrument_app(app)


def test_unhandled_exception_response_captured(
    app: Starlette, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch, log_response_headers=True, log_response_body=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/error")
    assert response.status_code == 500
    (span,) = exported_spans(exporters)
    attributes = unwrap(span.attributes)
    assert attributes["http.response.header.content-type"] == ["text/plain; charset=utf-8"]
    assert attributes["apitally.response.body"] == "Internal Server Error"
    assert attributes["http.response.body.size"] == len("Internal Server Error")


def test_pre_instrumented_started_app_rebuilds_middleware_stack(
    app: Starlette, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    StarletteInstrumentor.instrument_app(app)
    with TestClient(app):
        pass  # first startup builds the middleware stack
    init(app, monkeypatch)

    with TestClient(app) as client:
        client.get("/items/42")
    (span,) = exported_spans(exporters)
    assert span.kind == SpanKind.SERVER
    response_body_size = unwrap(span.attributes)["http.response.body.size"]
    assert isinstance(response_body_size, int) and response_body_size > 0


def test_pre_instrumented_app_adapts_without_duplicate_spans(
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


@pytest.mark.parametrize("pre_instrumented", [False, True])
async def test_request_with_leaked_context_still_exports_server_span(
    app: Starlette, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch, pre_instrumented: bool
):
    if pre_instrumented:
        StarletteInstrumentor.instrument_app(app)
    init(app, monkeypatch)
    _, token = attach_stale_server_span()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            assert (await client.get("/items/42")).status_code == 200
    finally:
        otel_context.detach(token)

    (span,) = exported_spans(exporters)
    assert span.kind == SpanKind.SERVER
    assert span.parent is None
