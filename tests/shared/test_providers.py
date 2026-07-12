import gzip
import logging
import uuid

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

from apitally.shared import activation, export, providers
from apitally.shared import metrics as apitally_metrics
from apitally.shared.config import set_config
from tests.conftest import CONTRIB_SCOPE, WRITE_TOKEN, StubOTLPServer, unwrap


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


def test_export_headers_match_resource_env_with_and_without_user_provider():
    set_config(write_token=WRITE_TOKEN, env="staging")

    env = providers.resolve_env(None)
    resource = providers.create_resource(env)
    headers = providers.export_headers(env)
    assert headers["Apitally-Env"] == resource.attributes["deployment.environment.name"] == "staging"
    assert headers["Authorization"] == f"Bearer {WRITE_TOKEN}"
    assert providers.endpoint_url("/v1/traces") == "https://otlp.apitally.io/v1/traces"

    user_provider = TracerProvider(resource=Resource.create({"deployment.environment.name": "production"}))
    env = providers.resolve_env(user_provider)
    assert providers.export_headers(env)["Apitally-Env"] == "production"


def test_endpoint_override_ignores_otel_env_vars(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APITALLY_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example.com")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://collector.example.com/v1/traces")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-other=1")
    set_config(write_token=WRITE_TOKEN)

    assert providers.endpoint_url("/v1/traces") == "http://localhost:4318/v1/traces"
    assert providers.endpoint_url("/v1/metrics") == "http://localhost:4318/v1/metrics"
    assert providers.endpoint_url("/v1/logs") == "http://localhost:4318/v1/logs"
    assert "x-other" not in providers.export_headers("prod")


def test_pipeline_delivers_to_otlp_endpoint(otlp_server: StubOTLPServer, monkeypatch: pytest.MonkeyPatch):
    # Runs the real pipeline against a local HTTP server: spans, logs and metrics reach
    # the spool and one export cycle delivers them as gzipped protobuf
    monkeypatch.setenv("APITALLY_OTLP_ENDPOINT", otlp_server.url)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(export, "INITIAL_EXPORT_DELAY", 60.0)
    activation.configure(write_token=WRITE_TOKEN, env="ci")
    activation.activate()

    with trace.get_tracer(CONTRIB_SCOPE).start_as_current_span("GET /items", kind=SpanKind.SERVER):
        logging.getLogger("myapp").warning("hello")
    apitally_metrics.record_request(method="GET", route="/items", status_code=200, consumer=None, duration=0.1)
    unwrap(activation.export_worker).run_cycle(None)

    assert set(otlp_server.paths()) == {"/v1/traces", "/v1/metrics", "/v1/logs"}
    for _, headers, _ in otlp_server.requests:
        assert headers["Authorization"] == f"Bearer {WRITE_TOKEN}"
        assert headers["Apitally-Env"] == "ci"
        assert headers["Content-Encoding"] == "gzip"

    (trace_body,) = [body for path, _, body in otlp_server.requests if path == "/v1/traces"]
    trace_request = ExportTraceServiceRequest.FromString(gzip.decompress(trace_body))
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
