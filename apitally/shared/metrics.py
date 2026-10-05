import logging
import math
import threading
import time
from typing import NamedTuple

import psutil
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.metrics.v1.metrics_pb2 import AGGREGATION_TEMPORALITY_DELTA, ScopeMetrics
from opentelemetry.sdk.resources import Resource

from apitally.shared.spool import Spool
from apitally.shared.trace_encoder import add_attributes


logger = logging.getLogger(__name__)

HISTOGRAM_SCALE = 3
SCALE_FACTOR = math.ldexp(1 / math.log(2), HISTOGRAM_SCALE)
# Guards against misuse such as a request ID used as the consumer identifier
MAX_COMBINATIONS = 50_000
# Keeps each appended request well below the spool's 4 MB rotation threshold
COMBINATIONS_PER_REQUEST = 1_000
SCOPE_NAME = "apitally"
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
    request, scope_metrics = create_request(collect_resource)
    for name, unit, value in (
        ("process.uptime", "s", time.time() - process.create_time()),
        ("process.cpu.utilization", "1", process.cpu_percent() / 100 / (psutil.cpu_count() or 1)),
    ):
        scope_metrics.metrics.add(name=name, unit=unit).gauge.data_points.add(time_unix_nano=end_ns, as_double=value)
    scope_metrics.metrics.add(name="process.memory.usage", unit="By").gauge.data_points.add(
        time_unix_nano=end_ns, as_int=process.memory_info().rss
    )
    spool.append("metrics", request.SerializeToString())

    combinations = [(get_attributes(key), histograms) for key, histograms in collected.items()]
    for slice_start in range(0, len(combinations), COMBINATIONS_PER_REQUEST):
        request, scope_metrics = create_request(collect_resource)
        for histogram_index, (name, unit) in enumerate(HISTOGRAM_METRICS):
            metric = None
            for attributes, histograms in combinations[slice_start : slice_start + COMBINATIONS_PER_REQUEST]:
                histogram = histograms[histogram_index]
                if not histogram.count:
                    continue
                if metric is None:
                    metric = scope_metrics.metrics.add(name=name, unit=unit)
                    metric.exponential_histogram.aggregation_temporality = AGGREGATION_TEMPORALITY_DELTA
                data_point = metric.exponential_histogram.data_points.add(
                    start_time_unix_nano=start_ns,
                    time_unix_nano=end_ns,
                    count=histogram.count,
                    sum=histogram.sum,
                    scale=HISTOGRAM_SCALE,
                    zero_count=histogram.zero_count,
                    min=histogram.min,
                    max=histogram.max,
                )
                add_attributes(data_point.attributes, attributes)
                if histogram.bucket_counts:
                    offset = min(histogram.bucket_counts)
                    bucket_counts = [0] * (max(histogram.bucket_counts) - offset + 1)
                    for index, count in histogram.bucket_counts.items():
                        bucket_counts[index - offset] = count
                    data_point.positive.offset = offset
                    data_point.positive.bucket_counts.extend(bucket_counts)
        spool.append("metrics", request.SerializeToString())


def reset() -> None:
    global resource, process, interval_start_ns, capacity_warned, request_metrics_lock, request_histograms
    resource = None
    process = None
    interval_start_ns = 0
    capacity_warned = False
    request_metrics_lock = threading.Lock()
    request_histograms = {}


def create_request(collect_resource: Resource) -> tuple[ExportMetricsServiceRequest, ScopeMetrics]:
    request = ExportMetricsServiceRequest()
    resource_metrics = request.resource_metrics.add(schema_url=collect_resource.schema_url)
    add_attributes(resource_metrics.resource.attributes, collect_resource.attributes)
    scope_metrics = resource_metrics.scope_metrics.add()
    scope_metrics.scope.name = SCOPE_NAME
    return request, scope_metrics


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
