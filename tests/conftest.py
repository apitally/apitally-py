from dataclasses import dataclass, field
from typing import Any

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, MetricsData
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.test.globals_test import reset_trace_globals

from apitally.shared import activation, config, metrics, providers


@pytest.fixture(autouse=True)
def reset_apitally_config():
    yield
    config.reset()


@pytest.fixture(autouse=True)
def reset_otel_trace_globals():
    yield
    reset_trace_globals()
    providers.reset()


@pytest.fixture(autouse=True)
def reset_apitally_activation():
    yield
    activation.reset()


class InMemoryMetricExporter(MetricExporter):
    def export(self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any) -> MetricExportResult:
        return MetricExportResult.SUCCESS

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        pass

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True


@dataclass
class CreatedExporters:
    span: list[InMemorySpanExporter] = field(default_factory=list)
    log: list[InMemoryLogRecordExporter] = field(default_factory=list)
    metric: list[InMemoryMetricExporter] = field(default_factory=list)


@pytest.fixture
def memory_exporters(monkeypatch):
    """Replace the OTLP exporter factories so activation never constructs network exporters."""
    created = CreatedExporters()

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
