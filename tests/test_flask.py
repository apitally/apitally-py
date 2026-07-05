import json
from typing import Any, Iterator, NoReturn

import pytest
from flask import Flask, Response, jsonify
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.sdk.metrics.export import ExponentialHistogram, InMemoryMetricReader, Metric
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind

from apitally.flask import init_apitally
from apitally.shared import activation, metrics
from apitally.shared.consumer import set_consumer
from apitally.shared.redaction import REDACTED
from tests.conftest import CreatedExporters


TOKEN = "apt_" + "a" * 24


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

    yield app
    if getattr(app, "_is_instrumented_by_opentelemetry", False):
        FlaskInstrumentor.uninstrument_app(app)


def init(app: Flask, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    init_apitally(app, write_token=TOKEN, **kwargs)


def activate_with_metric_reader() -> InMemoryMetricReader:
    activation.activate()
    assert metrics.meter_provider is not None
    reader = InMemoryMetricReader(**metrics.HISTOGRAM_OVERRIDES)
    metrics.meter_provider.add_metric_reader(reader)
    return reader


def exported_spans(memory_exporters: CreatedExporters) -> list[ReadableSpan]:
    assert activation.span_processor is not None
    activation.span_processor.force_flush()
    return [span for exporter in memory_exporters.span for span in exporter.get_finished_spans()]


def collect_metric(reader: InMemoryMetricReader, name: str) -> Metric | None:
    metrics_data = reader.get_metrics_data()
    if metrics_data is None:
        return None
    for resource_metrics in metrics_data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == name:
                    return metric
    return None


def test_first_request_activates_and_is_recorded(
    app: Flask, memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)
    assert not activation.is_activated()

    response = app.test_client().get("/items/42")

    assert response.status_code == 200
    assert activation.is_activated()
    (span,) = exported_spans(memory_exporters)
    assert span.kind == SpanKind.SERVER
    attributes = dict(span.attributes or {})
    assert attributes["http.route"] == "/items/<int:item_id>"
    assert attributes["http.response.status_code"] == 200


def test_bodies_captured_while_span_recording(
    app: Flask, memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch, log_request_body=True, log_response_body=True)

    response = app.test_client().post("/items", json={"password": "secret123", "name": "x"})

    assert response.status_code == 200
    (span,) = exported_spans(memory_exporters)
    attributes = dict(span.attributes or {})
    # Presence on the exported span proves both writes landed before teardown ended the span
    assert json.loads(str(attributes["apitally.request.body"])) == {"password": REDACTED, "name": "x"}
    assert json.loads(str(attributes["apitally.response.body"])) == {"id": 1, "token": REDACTED}


def test_streaming_response_uncaptured_but_recorded(
    app: Flask, memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch, log_response_body=True)
    reader = activate_with_metric_reader()

    response = app.test_client().get("/stream")

    assert response.data == b'{"a": 1}'
    (span,) = exported_spans(memory_exporters)
    attributes = dict(span.attributes or {})
    assert "apitally.response.body" not in attributes
    assert "http.response.body.size" not in attributes
    duration_metric = collect_metric(reader, "http.server.request.duration")
    assert duration_metric is not None
    assert isinstance(duration_metric.data, ExponentialHistogram)
    (point,) = duration_metric.data.data_points
    assert (point.attributes or {})["http.route"] == "/stream"


def test_generator_response_not_flattened_by_capture(
    app: Flask, memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch
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

    # after_request hooks run in reverse registration order, so this runs after the capture hook
    @app.after_request
    def check(response: Response) -> Response:
        streamed_after_capture["value"] = response.is_streamed and not consumed
        return response

    init(app, monkeypatch, log_response_body=True)

    response = app.test_client().get("/gen")

    assert response.data == b'{"a": 1}'
    assert streamed_after_capture["value"] is True
    (span,) = exported_spans(memory_exporters)
    attributes = dict(span.attributes or {})
    assert attributes["http.route"] == "/gen"
    assert "apitally.response.body" not in attributes


def test_response_headers_captured_wire_final(
    app: Flask, memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch, log_response_headers=True)

    app.test_client().get("/headers")

    (span,) = exported_spans(memory_exporters)
    attributes = dict(span.attributes or {})
    assert attributes["http.response.header.x-custom"] == ("value",)
    # Content-Length is added by werkzeug after the view returns, proving wire-final capture
    assert "http.response.header.content-length" in attributes


def test_consumer_set_in_route_reaches_span_and_histogram(
    app: Flask, memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch
):
    init(app, monkeypatch)
    reader = activate_with_metric_reader()

    response = app.test_client().get("/consumer")

    # Consume the body; the transport records metrics when the response iterable completes
    assert response.get_json() == {"ok": True}
    (span,) = exported_spans(memory_exporters)
    assert dict(span.attributes or {})["apitally.consumer.identifier"] == "tester"
    duration_metric = collect_metric(reader, "http.server.request.duration")
    assert duration_metric is not None
    assert isinstance(duration_metric.data, ExponentialHistogram)
    (point,) = duration_metric.data.data_points
    assert (point.attributes or {})["apitally.consumer.identifier"] == "tester"


def test_init_apitally_swallows_instrumentation_errors(app: Flask, monkeypatch: pytest.MonkeyPatch):
    def raise_error(*args: Any, **kwargs: Any) -> NoReturn:
        raise RuntimeError("instrumentation failed")

    monkeypatch.setattr(FlaskInstrumentor, "instrument_app", raise_error)
    init_apitally(app, write_token=TOKEN)

    response = app.test_client().get("/items/1")

    assert response.status_code == 200


def test_pre_instrumented_app_adapts_without_double_spans(
    app: Flask, memory_exporters: CreatedExporters, monkeypatch: pytest.MonkeyPatch
):
    FlaskInstrumentor().instrument_app(app)
    init(app, monkeypatch, log_response_headers=True)

    response = app.test_client().get("/items/7")

    assert response.status_code == 200
    (span,) = exported_spans(memory_exporters)
    attributes = dict(span.attributes or {})
    assert attributes["http.route"] == "/items/<int:item_id>"
    # Transport glue still lands on the user-instrumented span
    assert "http.response.header.content-type" in attributes
