from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.metrics import CallbackOptions, Histogram, Observation
from opentelemetry.sdk.metrics import Histogram as SDKHistogram
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import AggregationTemporality, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExponentialBucketHistogramAggregation
from opentelemetry.sdk.resources import Resource

from apitally.shared.providers import create_meter_provider, create_metric_exporter


EXPORT_INTERVAL_MILLIS = 60_000

# Keyed on the SDK instrument class (the API class raises at reader construction), so the
# process gauges keep their default aggregation and temporality (design.md section 4)
HISTOGRAM_OVERRIDES: dict[str, Any] = {
    "preferred_temporality": {SDKHistogram: AggregationTemporality.DELTA},
    "preferred_aggregation": {SDKHistogram: ExponentialBucketHistogramAggregation(max_scale=3)},
}

# None values: these two instruments emit a single unlabeled observation each (design.md section 12)
SYSTEM_METRICS_CONFIG: dict[str, list[str] | None] = {
    "process.cpu.utilization": None,
    "process.memory.usage": None,
}

meter_provider: MeterProvider | None = None
reader: ApitallyMetricReader | None = None
request_duration: Histogram | None = None
request_body_size: Histogram | None = None
response_body_size: Histogram | None = None
start_time: float = 0.0

# OTel's fork handlers hold weak references to readers; keep detached instances
# alive to avoid unraisable noise on a later fork (design.md section 7)
detached_readers: list[ApitallyMetricReader] = []


def setup(resource: Resource) -> MeterProvider:
    global meter_provider, request_duration, request_body_size, response_body_size, start_time
    meter_provider = create_meter_provider(resource, metric_readers=[])
    start_time = time.monotonic()
    meter = meter_provider.get_meter("apitally")
    request_duration = meter.create_histogram("http.server.request.duration", unit="s")
    request_body_size = meter.create_histogram("http.server.request.body.size", unit="By")
    response_body_size = meter.create_histogram("http.server.response.body.size", unit="By")
    meter.create_observable_gauge("process.uptime", callbacks=[observe_uptime], unit="s")
    instrumentor = SystemMetricsInstrumentor(config=SYSTEM_METRICS_CONFIG)
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    instrumentor.instrument(meter_provider=meter_provider)
    return meter_provider


def record_request(
    method: str,
    route: str,
    status_code: int,
    consumer: str | None,
    duration: float,
    request_size: int | None = None,
    response_size: int | None = None,
    scheme: str | None = None,
) -> None:
    if request_duration is None or request_body_size is None or response_body_size is None:
        return
    method = method.upper()
    if method == "OPTIONS" or not route:
        return
    attributes: dict[str, str | int] = {
        "http.request.method": method,
        "http.route": route,
        "http.response.status_code": status_code,
    }
    if consumer:
        attributes["apitally.consumer.identifier"] = consumer
    if scheme:
        attributes["url.scheme"] = scheme
    if status_code >= 500:
        attributes["error.type"] = str(status_code)
    request_duration.record(duration, attributes)
    if request_size is not None and request_size >= 0:
        request_body_size.record(request_size, attributes)
    if response_size is not None and response_size >= 0:
        response_body_size.record(response_size, attributes)


def attach_reader(env: str) -> None:
    global reader
    if meter_provider is None:
        return
    detach_reader()
    # Interval passed explicitly so OTEL_METRIC_EXPORT_INTERVAL never applies; the 60 s
    # cadence is the liveness heartbeat (design.md section 4, spec section 7.3)
    exporter = create_metric_exporter(env, **HISTOGRAM_OVERRIDES)
    reader = ApitallyMetricReader(exporter, export_interval_millis=EXPORT_INTERVAL_MILLIS)
    meter_provider.add_metric_reader(reader)


def detach_reader() -> None:
    global reader
    if meter_provider is not None and reader is not None:
        meter_provider.remove_metric_reader(reader)
        detached_readers.append(reader)
        reader = None


def reset() -> None:
    global meter_provider, request_duration, request_body_size, response_body_size
    detach_reader()
    instrumentor = SystemMetricsInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    meter_provider = None
    request_duration = None
    request_body_size = None
    response_body_size = None


def observe_uptime(options: CallbackOptions) -> Iterable[Observation]:
    yield Observation(time.monotonic() - start_time)


class ApitallyMetricReader(PeriodicExportingMetricReader):
    def collect(self, timeout_millis: float = 10_000) -> None:  # ty: ignore[override-of-final-method]
        # Detaching nulls the collect callback; the final ticker collect must no-op
        # instead of logging a not-registered warning (design.md section 7)
        if self._collect is not None:
            super().collect(timeout_millis=timeout_millis)
