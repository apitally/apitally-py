import time
from collections.abc import Iterable
from typing import Any

from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.metrics import CallbackOptions, Histogram, Observation
from opentelemetry.sdk.metrics import Histogram as SDKHistogram
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import AggregationTemporality, MetricReader, MetricsData
from opentelemetry.sdk.metrics.view import Aggregation, ExponentialBucketHistogramAggregation
from opentelemetry.sdk.resources import Resource

from apitally.shared.providers import create_meter_provider
from apitally.shared.spool import Spool


# Keyed on the SDK instrument class (the API class raises at reader construction), so the
# process gauges keep their default aggregation and temporality
HISTOGRAM_PREFERRED_TEMPORALITY: dict[type, AggregationTemporality] = {SDKHistogram: AggregationTemporality.DELTA}
HISTOGRAM_PREFERRED_AGGREGATION: dict[type, Aggregation] = {
    SDKHistogram: ExponentialBucketHistogramAggregation(max_scale=3)
}

# None values: these two instruments emit a single unlabeled observation each
SYSTEM_METRICS_CONFIG: dict[str, list[str] | None] = {
    "process.cpu.utilization": None,
    "process.memory.usage": None,
}


class ApitallyMetricReader(MetricReader):
    """Non-periodic reader without a timer thread: the export worker drives collection
    every export cycle, so all three signals export in lockstep."""

    def __init__(self, spool: Spool) -> None:
        super().__init__(
            preferred_temporality=HISTOGRAM_PREFERRED_TEMPORALITY,
            preferred_aggregation=HISTOGRAM_PREFERRED_AGGREGATION,
        )
        self.spool = spool

    def collect(self, timeout_millis: float = 10_000) -> None:  # ty: ignore[override-of-final-method]
        if self._collect is not None:
            super().collect(timeout_millis=timeout_millis)

    def _receive_metrics(self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any) -> None:
        if metrics_data is None or not metrics_data.resource_metrics:  # pragma: no cover
            return
        self.spool.append("metrics", encode_metrics(metrics_data).SerializeToString())

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        pass


meter_provider: MeterProvider | None = None
reader: MetricReader | None = None
request_duration: Histogram | None = None
request_body_size: Histogram | None = None
response_body_size: Histogram | None = None
start_time: float = 0.0


def setup(resource: Resource, metric_reader: MetricReader, *additional_metric_readers: MetricReader) -> MeterProvider:
    global meter_provider, reader, request_duration, request_body_size, response_body_size, start_time
    reader = metric_reader
    meter_provider = create_meter_provider(resource, metric_readers=[reader, *additional_metric_readers])
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


def reset() -> None:
    global meter_provider, reader, request_duration, request_body_size, response_body_size
    if meter_provider is not None:
        meter_provider.shutdown()
    instrumentor = SystemMetricsInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    meter_provider = None
    reader = None
    request_duration = None
    request_body_size = None
    response_body_size = None


def observe_uptime(options: CallbackOptions) -> Iterable[Observation]:
    yield Observation(time.monotonic() - start_time)
