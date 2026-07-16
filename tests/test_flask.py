import json
from typing import Any, Iterator

import pytest
from flask import Blueprint, Flask, Response, jsonify
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind

import apitally
from apitally.shared import activation
from apitally.shared.consumer import set_consumer
from apitally.shared.redaction import REDACTED
from tests.conftest import (
    WRITE_TOKEN,
    InMemoryExporters,
    attach_metric_reader,
    duration_data_points,
    exported_spans,
    startup_payload,
    unwrap,
)


@pytest.fixture()
def app() -> Iterator[Flask]:
    app = Flask("test")

    @app.get("/items/<int:item_id>")
    def get_item(item_id: int) -> Response:
        return jsonify({"id": item_id})

    @app.post("/items")
    def create_item() -> Response:
        return jsonify({"id": 1, "token": "abc123"})

    @app.get("/stream")
    def stream() -> Response:
        return Response(iter([b'{"a":', b" 1}"]), mimetype="application/json", direct_passthrough=True)

    @app.get("/headers")
    def headers() -> tuple[Response, int, dict[str, str]]:
        return jsonify({"ok": True}), 200, {"X-Custom": "value"}

    @app.get("/consumer")
    def consumer() -> Response:
        set_consumer("tester")
        return jsonify({"ok": True})

    @app.get("/error")
    def error() -> Response:
        raise ValueError("boom")

    yield app
    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        FlaskInstrumentor.uninstrument_app(app)


def init(app: Flask, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    apitally.init(app, write_token=WRITE_TOKEN, **kwargs)


def activate_with_metric_reader() -> InMemoryMetricReader:
    activation.activate()
    return attach_metric_reader()


def test_blueprint_route_includes_url_prefix(app: Flask, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    blueprint = Blueprint("api", __name__, url_prefix="/api")

    @blueprint.get("/things/<int:thing_id>")
    def get_thing(thing_id: int) -> Response:
        return jsonify({"id": thing_id})

    app.register_blueprint(blueprint)
    init(app, monkeypatch)
    reader = activate_with_metric_reader()

    response = app.test_client().get("/api/things/7")

    # Consume the body; telemetry is recorded when the response iterable completes
    assert response.get_json() == {"id": 7}
    (span,) = exported_spans(exporters)
    (point,) = duration_data_points(reader)
    assert unwrap(span.attributes)["http.route"] == "/api/things/<int:thing_id>"
    assert unwrap(point.attributes)["http.route"] == "/api/things/<int:thing_id>"


def test_first_request_activates_and_is_recorded(
    app: Flask, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)
    assert not activation.is_activated()

    response = app.test_client().get("/items/42")

    assert response.get_json() == {"id": 42}
    assert activation.is_activated()
    (span,) = exported_spans(exporters)
    assert span.kind == SpanKind.SERVER
    attributes = dict(span.attributes or {})
    assert attributes["http.route"] == "/items/<int:item_id>"
    assert attributes["http.response.status_code"] == 200
    assert attributes["http.response.body.size"] == len(response.data)


def test_startup_event_paths_match_routes(app: Flask, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    init(app, monkeypatch, app_version="1.2.3")

    response = app.test_client().get("/items/42")

    assert response.status_code == 200
    payload = startup_payload(exporters)
    assert payload["framework"] == "flask"
    assert "flask" in payload["versions"]
    assert payload["versions"]["app"] == "1.2.3"
    # Exact list: pins the exclusion of HEAD/OPTIONS methods and the static route
    assert sorted(payload["paths"], key=lambda p: (p["path"], p["method"])) == [
        {"method": "GET", "path": "/consumer"},
        {"method": "GET", "path": "/error"},
        {"method": "GET", "path": "/headers"},
        {"method": "POST", "path": "/items"},
        {"method": "GET", "path": "/items/<int:item_id>"},
        {"method": "GET", "path": "/stream"},
    ]


def test_request_and_response_bodies_captured_and_redacted(
    app: Flask, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch, log_request_body=True, log_response_body=True)

    response = app.test_client().post("/items", json={"password": "secret123", "name": "x"})

    assert response.get_json() == {"id": 1, "token": "abc123"}
    (span,) = exported_spans(exporters)
    attributes = dict(span.attributes or {})
    assert json.loads(str(attributes["apitally.request.body"])) == {"password": REDACTED, "name": "x"}
    assert json.loads(str(attributes["apitally.response.body"])) == {"id": 1, "token": REDACTED}


def test_streaming_response_size_and_body_captured(
    app: Flask, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch, log_response_body=True)
    reader = activate_with_metric_reader()

    response = app.test_client().get("/stream")

    assert response.data == b'{"a": 1}'
    (span,) = exported_spans(exporters)
    attributes = dict(span.attributes or {})
    # No Content-Length; the size and body accumulated at the transport reach the span
    # after the instrumentor ended it
    assert attributes["http.response.body.size"] == len(response.data)
    assert json.loads(str(attributes["apitally.response.body"])) == {"a": 1}
    (point,) = duration_data_points(reader)
    assert (point.attributes or {})["http.route"] == "/stream"


def test_body_capture_does_not_consume_streaming_response_early(
    app: Flask, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    consumed = []

    @app.get("/gen")
    def gen() -> Response:
        def generate() -> Iterator[bytes]:
            consumed.append(True)
            yield b'{"a":'
            yield b" 1}"

        return Response(generate(), mimetype="application/json")

    streamed_after_capture = {}

    @app.after_request
    def check(response: Response) -> Response:
        streamed_after_capture["value"] = response.is_streamed and not consumed
        return response

    init(app, monkeypatch, log_response_body=True)

    response = app.test_client().get("/gen")

    assert response.data == b'{"a": 1}'
    # The generator was still unconsumed when the response left the app; capture happens
    # chunk by chunk at the transport
    assert streamed_after_capture["value"] is True
    (span,) = exported_spans(exporters)
    attributes = dict(span.attributes or {})
    assert attributes["http.route"] == "/gen"
    assert json.loads(str(attributes["apitally.response.body"])) == {"a": 1}


def test_response_headers_include_headers_added_by_framework(
    app: Flask, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch, log_response_headers=True)

    assert app.test_client().get("/headers").data

    (span,) = exported_spans(exporters)
    attributes = dict(span.attributes or {})
    assert attributes["http.response.header.x-custom"] == ["value"]
    # Content-Length is added by werkzeug after the view returns, so its presence proves
    # the headers were captured as sent, not as returned by the view
    assert "http.response.header.content-length" in attributes


def test_set_consumer_reaches_span_and_histogram(
    app: Flask, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)
    reader = activate_with_metric_reader()

    response = app.test_client().get("/consumer")

    assert response.get_json() == {"ok": True}
    (span,) = exported_spans(exporters)
    assert dict(span.attributes or {})["apitally.consumer.identifier"] == "tester"
    (point,) = duration_data_points(reader)
    assert (point.attributes or {})["apitally.consumer.identifier"] == "tester"


def test_init_twice_does_not_stack_middleware(
    app: Flask, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)
    wsgi_app = app.wsgi_app
    init(app, monkeypatch)
    assert app.wsgi_app is wsgi_app

    response = app.test_client().get("/items/42")
    assert response.get_json() == {"id": 42}
    (span,) = exported_spans(exporters)
    assert span.kind == SpanKind.SERVER


def test_sampled_out_request_skips_capture(app: Flask, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    mask_calls: list[bytes] = []

    def mask(span: ReadableSpan, body: bytes) -> bytes:
        mask_calls.append(body)
        return body

    init(
        app,
        monkeypatch,
        sample_rate=0.0,
        log_request_body=True,
        log_response_body=True,
        mask_request_body=mask,
        mask_response_body=mask,
    )
    reader = activate_with_metric_reader()

    response = app.test_client().post("/items", json={"a": 1})

    assert response.get_json() == {"id": 1, "token": "abc123"}
    assert not mask_calls
    assert exported_spans(exporters) == []
    (point,) = duration_data_points(reader)
    assert point.count == 1


def test_unhandled_exception_recorded_on_server_span(
    app: Flask, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)

    response = app.test_client().get("/error")

    assert response.status_code == 500
    assert response.data
    (span,) = exported_spans(exporters)
    attributes = dict(span.attributes or {})
    assert attributes["http.response.status_code"] == 500
    (event,) = [e for e in span.events if e.name == "exception"]
    assert (event.attributes or {})["exception.type"] == "ValueError"
    assert (event.attributes or {})["exception.message"] == "boom"


def test_pre_instrumented_app_adapts_without_duplicate_spans(
    app: Flask, exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    FlaskInstrumentor().instrument_app(app)
    init(app, monkeypatch, log_response_headers=True)

    response = app.test_client().get("/items/7")

    assert response.get_json() == {"id": 7}
    (span,) = exported_spans(exporters)
    attributes = dict(span.attributes or {})
    assert attributes["http.route"] == "/items/<int:item_id>"
    # The Apitally middleware still sets its attributes on the span created by the user's instrumentation
    assert "http.response.header.content-type" in attributes
