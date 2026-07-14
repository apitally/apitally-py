import gzip
import json
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import find_spec
from typing import Any, TypeVar, cast

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry._logs import LogRecord
from opentelemetry.instrumentation._semconv import _OpenTelemetrySemanticConventionStability
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    DataPointT,
    ExponentialHistogramDataPoint,
    InMemoryMetricReader,
    Metric,
)
from opentelemetry.sdk.trace import ReadableSpan, Span, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Sampler
from opentelemetry.test.globals_test import reset_trace_globals
from opentelemetry.trace import SpanKind, Tracer

from apitally.shared import activation, config, export, metrics, providers, startup
from apitally.shared.consumer import consumer_holder_var
from apitally.shared.context import server_span_kept_var, server_span_processor_var, server_span_var
from apitally.shared.exporter import ApitallySpanExporter
from apitally.shared.span_processor import ApitallySpanProcessor
from apitally.shared.spool import Spool, SpoolFile


WRITE_TOKEN = "apt_" + "a" * 24
CONTRIB_SCOPE = "opentelemetry.instrumentation.test"


def installed(*modules: str) -> bool:
    return all(find_spec(module) is not None for module in modules)


_T = TypeVar("_T")


def unwrap(value: _T | None) -> _T:
    assert value is not None
    return value


def read_spool_payload(file: SpoolFile) -> bytes:
    """Decompressed concatenation of the OTLP payloads appended to a spool file."""
    file.sink.seek(0)
    return gzip.decompress(file.sink.read())


def attach_stale_server_span() -> tuple[Span, Any]:
    """Mimics a pipelined request's task with the previous request's OTel context still attached."""
    stale_span = TracerProvider().get_tracer("test").start_span("GET /previous", kind=SpanKind.SERVER)
    stale_span.end()
    token = otel_context.attach(trace.set_span_in_context(stale_span))
    return cast(Span, stale_span), token


def configure_and_activate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    activation.configure(write_token=WRITE_TOKEN)
    activation.activate()
    assert activation.is_activated()


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
    # The instrumentation layer reads the env var once on the first instrument() call and
    # caches it process-globally; reset the cache so each test re-reads the current env var
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


@pytest.fixture(autouse=True)
def reset_context_vars() -> Iterator[None]:
    # Request-scoped ContextVars intentionally persist after a request; clear them between tests
    yield
    server_span_var.set(None)
    server_span_kept_var.set(False)
    server_span_processor_var.set(None)
    consumer_holder_var.set(None)


@dataclass
class InMemoryExporters:
    span: list[InMemorySpanExporter] = field(default_factory=list)
    log: list[InMemoryLogRecordExporter] = field(default_factory=list)


@pytest.fixture
def exporters(monkeypatch: pytest.MonkeyPatch) -> InMemoryExporters:
    """Replace the spool exporter factories with in-memory exporters and keep the export
    worker from starting its thread or sending, so activation and shutdown never perform
    network I/O."""
    created = InMemoryExporters()

    def span_exporter(spool: Spool) -> InMemorySpanExporter:
        exporter = InMemorySpanExporter()
        created.span.append(exporter)
        return exporter

    def log_exporter(spool: Spool) -> InMemoryLogRecordExporter:
        exporter = InMemoryLogRecordExporter()
        created.log.append(exporter)
        return exporter

    monkeypatch.setattr(export, "create_span_exporter", span_exporter)
    monkeypatch.setattr(export, "create_log_exporter", log_exporter)
    monkeypatch.setattr(export.ExportWorker, "start", lambda self: None)
    monkeypatch.setattr(export.ExportWorker, "send_pending", lambda self, stop_event: None)
    return created


@pytest.fixture()
def span_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture()
def tracer(span_exporter: InMemorySpanExporter) -> Tracer:
    """Tracer with the Apitally span processor attached directly to the in-memory exporter,
    skipping the Apitally exporter so tests observe processed spans without redaction."""
    provider = TracerProvider()
    provider.add_span_processor(ApitallySpanProcessor(SimpleSpanProcessor(span_exporter)))
    return provider.get_tracer(CONTRIB_SCOPE)


class StubOTLPServer:
    """Local HTTP server recording POSTed requests, with scriptable responses per path."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        self.respond: Callable[[str], tuple[int, dict[str, str]]] = lambda path: (200, {})
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                stub.requests.append((self.path, dict(self.headers), body))
                status, headers = stub.respond(self.path)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def paths(self) -> list[str]:
        return [path for path, _, _ in self.requests]


@pytest.fixture
def otlp_server() -> Iterator[StubOTLPServer]:
    stub = StubOTLPServer()
    yield stub
    stub.server.shutdown()
    stub.server.server_close()


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


def create_tracer(exporter: SpanExporter, sampler: Sampler = ALWAYS_ON, scope: str = CONTRIB_SCOPE) -> Tracer:
    # The Apitally span processor and exporter bind config at construction, so build after configure()
    provider = TracerProvider(sampler=sampler)
    provider.add_span_processor(ApitallySpanProcessor(SimpleSpanProcessor(ApitallySpanExporter(exporter))))
    return provider.get_tracer(scope)


def create_trace_pipeline(
    sampler: Sampler = ALWAYS_ON, scope: str = CONTRIB_SCOPE
) -> tuple[Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    return create_tracer(exporter, sampler=sampler, scope=scope), exporter
