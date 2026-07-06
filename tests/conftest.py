import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.util import find_spec
from typing import Any, TypeVar

import pytest
from opentelemetry._logs import LogRecord
from opentelemetry.instrumentation._semconv import _OpenTelemetrySemanticConventionStability
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    DataPointT,
    ExponentialHistogramDataPoint,
    InMemoryMetricReader,
    Metric,
    MetricExporter,
    MetricExportResult,
    MetricsData,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.test.globals_test import reset_trace_globals
from opentelemetry.trace import SpanKind

from apitally.shared import activation, config, metrics, providers, startup


def installed(*modules: str) -> bool:
    return all(find_spec(module) is not None for module in modules)


_T = TypeVar("_T")


def unwrap(value: _T | None) -> _T:
    assert value is not None
    return value


# Skip collection of framework test modules whose framework or instrumentor is not installed,
# so the CI test matrix can run with a single framework at a time
collect_ignore = []
if not installed("blacksheep", "opentelemetry.instrumentation.asgi"):
    collect_ignore.append("test_blacksheep.py")
if not installed("django", "opentelemetry.instrumentation.django"):
    collect_ignore.extend(["test_django.py", "test_django_ninja.py", "test_django_rest_framework.py"])
if not installed("ninja"):
    collect_ignore.append("test_django_ninja.py")
if not installed("rest_framework"):
    collect_ignore.append("test_django_rest_framework.py")
if not installed("fastapi", "opentelemetry.instrumentation.fastapi"):
    collect_ignore.append("test_fastapi.py")
if not installed("flask", "opentelemetry.instrumentation.flask"):
    collect_ignore.append("test_flask.py")
if not installed("litestar", "opentelemetry.instrumentation.asgi"):
    collect_ignore.append("test_litestar.py")
if not installed("starlette", "opentelemetry.instrumentation.starlette"):
    collect_ignore.append("test_starlette.py")


@pytest.fixture(autouse=True)
def reset_apitally_config() -> Iterator[None]:
    # configure() sets OTEL_SEMCONV_STABILITY_OPT_IN via setdefault; restore it so the
    # value never leaks between tests
    semconv_before = os.environ.get("OTEL_SEMCONV_STABILITY_OPT_IN")
    yield
    config.reset()
    if semconv_before is None:
        os.environ.pop("OTEL_SEMCONV_STABILITY_OPT_IN", None)
    else:
        os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = semconv_before
    # The instrumentation layer reads the env var once into a process-global latch on the
    # first instrument() call; reset it so each test re-reads the current env var
    _OpenTelemetrySemanticConventionStability._initialized = False
    _OpenTelemetrySemanticConventionStability._OTEL_SEMCONV_STABILITY_SIGNAL_MAPPING = {}


@pytest.fixture(autouse=True)
def reset_otel_trace_globals() -> Iterator[None]:
    yield
    reset_trace_globals()
    providers.reset()


@pytest.fixture(autouse=True)
def reset_apitally_activation() -> Iterator[None]:
    yield
    activation.reset()
    startup.reset()


class InMemoryMetricExporter(MetricExporter):
    def export(self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any) -> MetricExportResult:
        return MetricExportResult.SUCCESS

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        pass

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True


@dataclass
class InMemoryExporters:
    span: list[InMemorySpanExporter] = field(default_factory=list)
    log: list[InMemoryLogRecordExporter] = field(default_factory=list)
    metric: list[InMemoryMetricExporter] = field(default_factory=list)


@pytest.fixture
def exporters(monkeypatch: pytest.MonkeyPatch) -> InMemoryExporters:
    """Replace the OTLP exporter factories so activation never constructs network exporters."""
    created = InMemoryExporters()

    def span_exporter(env: str) -> InMemorySpanExporter:
        exporter = InMemorySpanExporter()
        created.span.append(exporter)
        return exporter

    def log_exporter(env: str) -> InMemoryLogRecordExporter:
        exporter = InMemoryLogRecordExporter()
        created.log.append(exporter)
        return exporter

    def metric_exporter(env: str, **kwargs: Any) -> InMemoryMetricExporter:
        exporter = InMemoryMetricExporter(**kwargs)
        created.metric.append(exporter)
        return exporter

    monkeypatch.setattr(providers, "create_span_exporter", span_exporter)
    monkeypatch.setattr(providers, "create_log_exporter", log_exporter)
    monkeypatch.setattr(metrics, "create_metric_exporter", metric_exporter)
    return created


def attach_metric_reader(provider: MeterProvider | None = None) -> InMemoryMetricReader:
    """Attach an in-memory reader with the same histogram temporality and aggregation as production."""
    if provider is None:
        provider = unwrap(metrics.meter_provider)
    reader = InMemoryMetricReader(
        preferred_temporality=metrics.HISTOGRAM_PREFERRED_TEMPORALITY,
        preferred_aggregation=metrics.HISTOGRAM_PREFERRED_AGGREGATION,
    )
    provider.add_metric_reader(reader)
    return reader


def collect_metrics(reader: InMemoryMetricReader) -> dict[str, Metric]:
    metrics_data = reader.get_metrics_data()
    if metrics_data is None:
        return {}
    return {
        metric.name: metric
        for resource_metrics in metrics_data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }


def metric_data_points(reader: InMemoryMetricReader, name: str) -> list[DataPointT]:
    metric = collect_metrics(reader).get(name)
    return list(metric.data.data_points) if metric is not None else []


def duration_data_points(reader: InMemoryMetricReader) -> list[ExponentialHistogramDataPoint]:
    points = metric_data_points(reader, "http.server.request.duration")
    return [point for point in points if isinstance(point, ExponentialHistogramDataPoint)]


def exported_spans(exporters: InMemoryExporters, kind: SpanKind | None = None) -> list[ReadableSpan]:
    unwrap(activation.span_processor).force_flush()
    return [
        span
        for exporter in exporters.span
        for span in exporter.get_finished_spans()
        if kind is None or span.kind == kind
    ]


def exported_log_records(exporters: InMemoryExporters) -> list[LogRecord]:
    unwrap(activation.log_processor).force_flush()
    return [exported.log_record for exporter in exporters.log for exported in exporter.get_finished_logs()]


def startup_payload(exporters: InMemoryExporters) -> dict[str, Any]:
    (record,) = [r for r in exported_log_records(exporters) if r.event_name == startup.EVENT_NAME]
    assert isinstance(record.body, str)
    return json.loads(record.body)
