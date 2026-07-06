from collections.abc import Iterator

import pytest
from opentelemetry.sdk.metrics import Histogram as SDKHistogram
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    ExponentialHistogram,
    Gauge,
    InMemoryMetricReader,
    Metric,
    Sum,
)
from opentelemetry.sdk.metrics.view import ExponentialBucketHistogramAggregation
from opentelemetry.sdk.resources import Resource

from apitally.shared import metrics


@pytest.fixture(autouse=True)
def reset_metrics() -> Iterator[None]:
    yield
    metrics.reset()


def create_pipeline() -> InMemoryMetricReader:
    provider = metrics.setup(Resource.create({}))
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


def get_scope_names(reader: InMemoryMetricReader) -> dict[str, str]:
    metrics_data = reader.get_metrics_data()
    assert metrics_data is not None
    return {
        metric.name: scope_metrics.scope.name
        for resource_metrics in metrics_data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }


def test_request_histograms_exponential_delta_shape():
    reader = create_pipeline()
    metrics.record_request(
        "get", "/items/{id}", 200, consumer=None, duration=0.123, request_size=10, response_size=250, scheme="https"
    )
    collected = collect_metrics(reader)

    duration_metric = collected["http.server.request.duration"]
    assert duration_metric.unit == "s"
    assert isinstance(duration_metric.data, ExponentialHistogram)
    assert duration_metric.data.aggregation_temporality == AggregationTemporality.DELTA
    (point,) = duration_metric.data.data_points
    assert -2 <= point.scale <= 3
    assert point.count == 1
    assert point.attributes == {
        "http.request.method": "GET",
        "http.route": "/items/{id}",
        "http.response.status_code": 200,
        "url.scheme": "https",
    }

    for name in ("http.server.request.body.size", "http.server.response.body.size"):
        size_metric = collected[name]
        assert size_metric.unit == "By"
        assert isinstance(size_metric.data, ExponentialHistogram)
        (size_point,) = size_metric.data.data_points
        assert size_point.attributes == point.attributes


def test_histograms_under_apitally_scope():
    reader = create_pipeline()
    metrics.record_request("GET", "/items", 200, consumer=None, duration=0.1)
    assert get_scope_names(reader)["http.server.request.duration"] == "apitally"


def test_export_interval_ignores_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "5000")
    provider = metrics.setup(Resource.create({}))
    metrics.attach_reader("prod")
    reader = metrics.reader
    assert reader is not None
    assert reader in provider._all_metric_readers
    assert reader._export_interval_millis == 60_000
    # The reader re-keys the overrides onto internal instrument base classes
    histogram_key = next(cls for cls in reader._instrument_class_temporality if issubclass(cls, SDKHistogram))
    assert reader._instrument_class_temporality[histogram_key] == AggregationTemporality.DELTA
    aggregation = reader._instrument_class_aggregation[histogram_key]
    assert isinstance(aggregation, ExponentialBucketHistogramAggregation)
    assert aggregation._max_scale == 3


def test_consumer_identifier_attribute():
    reader = create_pipeline()
    metrics.record_request("GET", "/a", 200, consumer="tenant-1", duration=0.01)
    metrics.record_request("GET", "/b", 200, consumer=None, duration=0.01)
    duration_metric = collect_metrics(reader)["http.server.request.duration"]
    assert isinstance(duration_metric.data, ExponentialHistogram)
    by_route = {
        (point.attributes or {})["http.route"]: point.attributes or {} for point in duration_metric.data.data_points
    }
    assert by_route["/a"]["apitally.consumer.identifier"] == "tenant-1"
    assert "apitally.consumer.identifier" not in by_route["/b"]


def test_options_and_unmatched_route_not_recorded():
    reader = create_pipeline()
    metrics.record_request("OPTIONS", "/items", 204, consumer=None, duration=0.01)
    metrics.record_request("GET", "", 404, consumer=None, duration=0.01)
    assert "http.server.request.duration" not in collect_metrics(reader)


def test_excluded_request_still_recorded():
    # Exclusion filters spans only; the transport calls the helper for excluded requests too (spec section 6.8)
    reader = create_pipeline()
    metrics.record_request("GET", "/healthz", 200, consumer=None, duration=0.005)
    duration_metric = collect_metrics(reader)["http.server.request.duration"]
    assert isinstance(duration_metric.data, ExponentialHistogram)
    (point,) = duration_metric.data.data_points
    assert (point.attributes or {})["http.route"] == "/healthz"


def test_unknown_sizes_skip_size_observations():
    reader = create_pipeline()
    metrics.record_request("GET", "/a", 500, consumer=None, duration=0.01)
    collected = collect_metrics(reader)
    duration_metric = collected["http.server.request.duration"]
    assert isinstance(duration_metric.data, ExponentialHistogram)
    (point,) = duration_metric.data.data_points
    assert (point.attributes or {})["error.type"] == "500"
    assert "http.server.request.body.size" not in collected
    assert "http.server.response.body.size" not in collected


def test_system_metrics_exact_instrument_set():
    reader = create_pipeline()
    assert set(collect_metrics(reader)) == {"process.cpu.utilization", "process.memory.usage", "process.uptime"}


def test_cpu_and_memory_share_timestamp_with_empty_attributes():
    reader = create_pipeline()
    collected = collect_metrics(reader)
    (cpu_point,) = collected["process.cpu.utilization"].data.data_points
    (mem_point,) = collected["process.memory.usage"].data.data_points
    assert cpu_point.time_unix_nano == mem_point.time_unix_nano
    assert dict(cpu_point.attributes or {}) == {}
    assert dict(mem_point.attributes or {}) == {}


def test_process_gauges_keep_default_aggregation():
    reader = create_pipeline()
    collected = collect_metrics(reader)
    assert isinstance(collected["process.cpu.utilization"].data, Gauge)
    assert isinstance(collected["process.memory.usage"].data, Sum)
    assert isinstance(collected["process.uptime"].data, Gauge)
    (uptime_point,) = collected["process.uptime"].data.data_points
    assert uptime_point.value >= 0
