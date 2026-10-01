import logging
import math
import threading
import time
from typing import NamedTuple

import psutil
from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    Buckets,
    ExponentialHistogramDataPoint,
    Gauge,
    Metric,
    MetricsData,
    NumberDataPoint,
    ResourceMetrics,
    ScopeMetrics,
)
from opentelemetry.sdk.metrics.export import ExponentialHistogram as OtelExponentialHistogram
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from apitally.shared.spool import Spool


logger = logging.getLogger(__name__)

HISTOGRAM_SCALE = 3
SCALE_FACTOR = math.ldexp(1 / math.log(2), HISTOGRAM_SCALE)
# Guards against misuse such as a request ID used as the consumer identifier
MAX_COMBINATIONS = 50_000
# Keeps each appended request well below the spool's 4 MB rotation threshold
COMBINATIONS_PER_REQUEST = 1_000
SCOPE = InstrumentationScope("apitally")
# Name and unit for each field of RequestHistograms, in the same order
HISTOGRAM_METRICS = (
    ("http.server.request.duration", "s"),
    ("http.server.request.body.size", "By"),
    ("http.server.response.body.size", "By"),
)


class RequestMetricKey(NamedTuple):
    method: str
    route: str
    status_code: int
    scheme: str | None
    consumer: str | None


class ExponentialHistogram:
    __slots__ = ("count", "sum", "min", "max", "zero_count", "bucket_counts")

    def __init__(self) -> None:
        self.count = 0
        self.sum: float = 0
        self.min = math.inf
        self.max = -math.inf
        self.zero_count = 0
        self.bucket_counts: dict[int, int] = {}

    def record(self, value: float) -> None:
        self.count += 1
        self.sum += value
        if value < self.min:
            self.min = value
        if value > self.max:
            self.max = value
        if value == 0:
            self.zero_count += 1
        else:
            index = map_to_index(value)
            self.bucket_counts[index] = self.bucket_counts.get(index, 0) + 1


class RequestHistograms(NamedTuple):
    duration: ExponentialHistogram
    request_body_size: ExponentialHistogram
    response_body_size: ExponentialHistogram


resource: Resource | None = None
process: psutil.Process | None = None
interval_start_ns = 0
capacity_warned = False
request_metrics_lock = threading.Lock()
request_histograms: dict[RequestMetricKey, RequestHistograms] = {}


def setup(new_resource: Resource) -> None:
    global resource, process, interval_start_ns
    resource = new_resource
    process = psutil.Process()
    interval_start_ns = time.time_ns()


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
    if resource is None:
        return
    method = method.upper()
    if method == "OPTIONS" or not route:
        return
    key = RequestMetricKey(method, route, status_code, scheme, consumer)
    with request_metrics_lock:
        histograms = request_histograms.get(key)
        if histograms is None:
            if len(request_histograms) >= MAX_COMBINATIONS:
                return
            histograms = RequestHistograms(ExponentialHistogram(), ExponentialHistogram(), ExponentialHistogram())
            request_histograms[key] = histograms
        histograms.duration.record(duration)
        if request_size is not None and request_size >= 0:
            histograms.request_body_size.record(request_size)
        if response_size is not None and response_size >= 0:
            histograms.response_body_size.record(response_size)


def collect(spool: Spool) -> None:
    global request_histograms, interval_start_ns, capacity_warned
    collect_resource = resource
    if collect_resource is None or process is None:
        return
    with request_metrics_lock:
        collected = request_histograms
        request_histograms = {}
        start_ns = interval_start_ns
        end_ns = interval_start_ns = time.time_ns()
    if len(collected) >= MAX_COMBINATIONS and not capacity_warned:
        capacity_warned = True
        logger.warning(
            "Apitally request metrics exceeded the capacity of %s distinct attribute combinations per collection "
            "interval, so some request metrics are missing. See https://docs.apitally.io or contact support if "
            "this persists.",
            f"{MAX_COMBINATIONS:,}",
        )
    gauges = [
        ("process.uptime", "s", time.time() - process.create_time()),
        ("process.cpu.utilization", "1", process.cpu_percent() / 100 / (psutil.cpu_count() or 1)),
        ("process.memory.usage", "By", process.memory_info().rss),
    ]
    append_metrics(
        spool,
        collect_resource,
        [Metric(name, "", unit, Gauge([NumberDataPoint({}, 0, end_ns, value)])) for name, unit, value in gauges],
    )
    combinations = [(get_attributes(key), histograms) for key, histograms in collected.items()]
    for slice_start in range(0, len(combinations), COMBINATIONS_PER_REQUEST):
        combinations_slice = combinations[slice_start : slice_start + COMBINATIONS_PER_REQUEST]
        histogram_metrics = []
        for histogram_index, (name, unit) in enumerate(HISTOGRAM_METRICS):
            data_points = [
                create_data_point(attributes, histograms[histogram_index], start_ns, end_ns)
                for attributes, histograms in combinations_slice
                if histograms[histogram_index].count
            ]
            if data_points:
                histogram_metrics.append(
                    Metric(name, "", unit, OtelExponentialHistogram(data_points, AggregationTemporality.DELTA))
                )
        append_metrics(spool, collect_resource, histogram_metrics)


def reset() -> None:
    global resource, process, interval_start_ns, capacity_warned, request_metrics_lock, request_histograms
    resource = None
    process = None
    interval_start_ns = 0
    capacity_warned = False
    request_metrics_lock = threading.Lock()
    request_histograms = {}


def append_metrics(spool: Spool, collect_resource: Resource, metrics: list[Metric]) -> None:
    metrics_data = MetricsData([ResourceMetrics(collect_resource, [ScopeMetrics(SCOPE, metrics, "")], "")])
    spool.append("metrics", encode_metrics(metrics_data).SerializeToString())


def create_data_point(
    attributes: dict[str, str | int], histogram: ExponentialHistogram, start_ns: int, end_ns: int
) -> ExponentialHistogramDataPoint:
    bucket_counts = histogram.bucket_counts
    offset = min(bucket_counts, default=0)
    return ExponentialHistogramDataPoint(
        attributes=attributes,
        start_time_unix_nano=start_ns,
        time_unix_nano=end_ns,
        count=histogram.count,
        sum=histogram.sum,
        scale=HISTOGRAM_SCALE,
        zero_count=histogram.zero_count,
        positive=Buckets(
            offset, [bucket_counts.get(index, 0) for index in range(offset, max(bucket_counts, default=-1) + 1)]
        ),
        negative=Buckets(0, []),
        flags=0,
        min=histogram.min,
        max=histogram.max,
    )


def get_attributes(key: RequestMetricKey) -> dict[str, str | int]:
    attributes: dict[str, str | int] = {
        "http.request.method": key.method,
        "http.route": key.route,
        "http.response.status_code": key.status_code,
    }
    if key.consumer:
        attributes["apitally.consumer.identifier"] = key.consumer
    if key.scheme:
        attributes["url.scheme"] = key.scheme
    if key.status_code >= 500:
        attributes["error.type"] = str(key.status_code)
    return attributes


def map_to_index(value: float) -> int:
    mantissa, exponent = math.frexp(value)
    # Exact powers of two are the inclusive upper boundary of the bucket below
    if mantissa == 0.5:
        return ((exponent - 1) << HISTOGRAM_SCALE) - 1
    return math.floor(math.log(value) * SCALE_FACTOR)
