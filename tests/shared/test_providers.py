import logging
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from opentelemetry import metrics, trace
from opentelemetry._logs import get_logger_provider
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import SpanKind

from apitally.shared import activation, providers
from apitally.shared import metrics as apitally_metrics
from apitally.shared.config import set_config
from tests.conftest import CONTRIB_SCOPE, WRITE_TOKEN, unwrap


def test_user_tracer_provider_detection():
    assert providers.get_user_tracer_provider() is None
    user_provider = TracerProvider()
    trace.set_tracer_provider(user_provider)
    assert providers.get_user_tracer_provider() is user_provider


def test_setup_own_tracer_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    monkeypatch.setenv("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "100")
    monkeypatch.setenv("OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "100")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "test-service")
    set_config(write_token=WRITE_TOKEN, env="staging")

    env = providers.resolve_env(None)
    resource = providers.create_resource(env)
    exporter = InMemorySpanExporter()
    provider = providers.setup_tracer_provider(resource, SimpleSpanProcessor(exporter))

    assert trace.get_tracer_provider() is provider
    assert provider.sampler is ALWAYS_ON

    with trace.get_tracer("test").start_as_current_span("span") as span:
        span.set_attribute("body", "x" * 70_000)

    (exported,) = exporter.get_finished_spans()
    assert exported.attributes is not None
    assert len(str(exported.attributes["body"])) == 65_536

    attributes = exported.resource.attributes
    assert uuid.UUID(str(attributes["service.instance.id"]))
    assert attributes["deployment.environment.name"] == "staging"
    assert attributes["telemetry.distro.name"] == "apitally-py"
    assert attributes["telemetry.distro.version"]
    assert attributes["service.name"] == "test-service"


def test_attach_to_user_tracer_provider():
    set_config(write_token=WRITE_TOKEN)
    user_exporter = InMemorySpanExporter()
    user_provider = TracerProvider()
    user_provider.add_span_processor(SimpleSpanProcessor(user_exporter))
    trace.set_tracer_provider(user_provider)

    detected = providers.get_user_tracer_provider()
    assert detected is user_provider
    our_exporter = InMemorySpanExporter()
    providers.attach_to_tracer_provider(detected, SimpleSpanProcessor(our_exporter))

    with trace.get_tracer("test").start_as_current_span("span"):
        pass

    assert len(user_exporter.get_finished_spans()) == 1
    assert len(our_exporter.get_finished_spans()) == 1


def test_env_conflict_uses_user_resource_value():
    set_config(write_token=WRITE_TOKEN, env="staging")
    user_provider = TracerProvider(resource=Resource.create({"deployment.environment.name": "production"}))
    env = providers.resolve_env(user_provider)
    assert env == "production"


def test_apitally_env_header_matches_resource_with_and_without_user_provider():
    set_config(write_token=WRITE_TOKEN, env="staging")

    env = providers.resolve_env(None)
    resource = providers.create_resource(env)
    exporter = providers.create_span_exporter(env)
    assert exporter._headers["Apitally-Env"] == resource.attributes["deployment.environment.name"] == "staging"
    assert exporter._headers["Authorization"] == f"Bearer {WRITE_TOKEN}"
    assert exporter._endpoint == "https://otlp.apitally.io/v1/traces"

    user_provider = TracerProvider(resource=Resource.create({"deployment.environment.name": "production"}))
    env = providers.resolve_env(user_provider)
    log_exporter = providers.create_log_exporter(env)
    assert log_exporter._headers["Apitally-Env"] == "production"


def test_exporter_endpoint_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APITALLY_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example.com")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://collector.example.com/v1/traces")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-other=1")
    set_config(write_token=WRITE_TOKEN)

    span_exporter = providers.create_span_exporter("prod")
    metric_exporter = providers.create_metric_exporter("prod")
    log_exporter = providers.create_log_exporter("prod")
    assert span_exporter._endpoint == "http://localhost:4318/v1/traces"
    assert metric_exporter._endpoint == "http://localhost:4318/v1/metrics"
    assert log_exporter._endpoint == "http://localhost:4318/v1/logs"
    assert "x-other" not in span_exporter._headers


def test_exporters_deliver_to_otlp_endpoint(monkeypatch: pytest.MonkeyPatch):
    # Wire-level counterpart to the endpoint/header assertions above, which poke exporter
    # privates: real exporters posting protobuf over HTTP, so otel-sdk drift is caught
    received: dict[str, tuple[Any, bytes]] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            received[self.path] = (self.headers, body)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        monkeypatch.setenv("APITALLY_OTLP_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        activation.configure(write_token=WRITE_TOKEN, env="ci")
        activation.activate()

        with trace.get_tracer(CONTRIB_SCOPE).start_as_current_span("GET /items", kind=SpanKind.SERVER):
            logging.getLogger("myapp").warning("hello")
        apitally_metrics.record_request(method="GET", route="/items", status_code=200, consumer=None, duration=0.1)

        unwrap(activation.span_processor).force_flush()
        unwrap(activation.log_processor).force_flush()
        unwrap(apitally_metrics.reader).force_flush()
        server.shutdown()

    assert set(received) == {"/v1/traces", "/v1/metrics", "/v1/logs"}
    for headers, _ in received.values():
        assert headers["Authorization"] == f"Bearer {WRITE_TOKEN}"
        assert headers["Apitally-Env"] == "ci"

    trace_request = ExportTraceServiceRequest()
    trace_request.ParseFromString(received["/v1/traces"][1])
    (span,) = [s for rs in trace_request.resource_spans for ss in rs.scope_spans for s in ss.spans]
    assert span.name == "GET /items"


def test_private_meter_and_logger_providers():
    set_config(write_token=WRITE_TOKEN)
    resource = providers.create_resource("prod")

    reader = InMemoryMetricReader()
    meter_provider = providers.create_meter_provider(resource, [reader])
    meter_provider.get_meter("apitally").create_counter("test.counter").add(1)
    metrics_data = reader.get_metrics_data()
    assert metrics_data is not None
    assert metrics_data.resource_metrics[0].resource.attributes["service.instance.id"]

    log_exporter = InMemoryLogRecordExporter()
    logger_provider = providers.create_logger_provider(resource, [SimpleLogRecordProcessor(log_exporter)])
    logger_provider.get_logger("apitally").emit(body="hello")
    (log_record,) = log_exporter.get_finished_logs()
    assert log_record.log_record.body == "hello"
    assert log_record.resource.attributes["service.instance.id"]

    assert metrics.get_meter_provider() is not meter_provider
    assert get_logger_provider() is not logger_provider
