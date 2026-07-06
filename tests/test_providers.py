import uuid

import pytest
from opentelemetry import metrics, trace
from opentelemetry._logs import get_logger_provider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from apitally.shared import providers
from apitally.shared.config import configure


TOKEN = "apt_" + "a" * 24


def test_mode_detection():
    assert providers.get_user_tracer_provider() is None
    user_provider = TracerProvider()
    trace.set_tracer_provider(user_provider)
    assert providers.get_user_tracer_provider() is user_provider


def test_own_it_all_tracer_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    monkeypatch.setenv("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "100")
    monkeypatch.setenv("OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT", "100")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "test-service")
    configure(write_token=TOKEN, env="staging")

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


def test_cooperative_pipeline():
    configure(write_token=TOKEN)
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


def test_cooperative_env_conflict_uses_resource_value():
    configure(write_token=TOKEN, env="staging")
    user_provider = TracerProvider(resource=Resource.create({"deployment.environment.name": "production"}))
    env = providers.resolve_env(user_provider)
    assert env == "production"


def test_apitally_env_header_matches_resource_in_both_modes():
    configure(write_token=TOKEN, env="staging")

    env = providers.resolve_env(None)
    resource = providers.create_resource(env)
    exporter = providers.create_span_exporter(env)
    assert exporter._headers["Apitally-Env"] == resource.attributes["deployment.environment.name"] == "staging"
    assert exporter._headers["Authorization"] == f"Bearer {TOKEN}"
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
    configure(write_token=TOKEN)

    span_exporter = providers.create_span_exporter("prod")
    metric_exporter = providers.create_metric_exporter("prod")
    log_exporter = providers.create_log_exporter("prod")
    assert span_exporter._endpoint == "http://localhost:4318/v1/traces"
    assert metric_exporter._endpoint == "http://localhost:4318/v1/metrics"
    assert log_exporter._endpoint == "http://localhost:4318/v1/logs"
    assert "x-other" not in span_exporter._headers


def test_private_meter_and_logger_providers():
    configure(write_token=TOKEN)
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
