import logging
import threading

import pytest
from opentelemetry.proto.metrics.v1.metrics_pb2 import AGGREGATION_TEMPORALITY_DELTA, ExponentialHistogramDataPoint
from opentelemetry.sdk.resources import Resource

from apitally.shared import metrics
from tests.conftest import collect_data_points, collect_metrics, duration_data_points, point_attributes


HISTOGRAM_NAMES = ("http.server.request.duration", "http.server.request.body.size", "http.server.response.body.size")


@pytest.fixture(autouse=True)
def setup_metrics() -> None:
    metrics.setup(Resource.create({"service.name": "test-service"}))


def test_collection_exports_complete_histogram_points():
    for duration, response_size in ((0.1, 1000), (0.125, 1024), (0.125, 1025)):
        metrics.record_request(
            "get",
            "/items/{id}",
            200,
            consumer="tenant-1",
            duration=duration,
            request_size=0,
            response_size=response_size,
            scheme="https",
        )
    _, histogram_entry = collect_metrics().resource_metrics
    assert point_attributes(histogram_entry.resource)["service.name"] == "test-service"
    (scope_metrics,) = histogram_entry.scope_metrics
    assert scope_metrics.scope.name == "apitally"
    collected = {metric.name: metric for metric in scope_metrics.metrics}
    assert {name: metric.unit for name, metric in collected.items()} == {
        "http.server.request.duration": "s",
        "http.server.request.body.size": "By",
        "http.server.response.body.size": "By",
    }
    assert all(
        metric.exponential_histogram.aggregation_temporality == AGGREGATION_TEMPORALITY_DELTA
        for metric in collected.values()
    )
    (duration_point,) = collected["http.server.request.duration"].exponential_histogram.data_points
    (request_size_point,) = collected["http.server.request.body.size"].exponential_histogram.data_points
    (response_size_point,) = collected["http.server.response.body.size"].exponential_histogram.data_points
    assert point_attributes(duration_point) == {
        "http.request.method": "GET",
        "http.route": "/items/{id}",
        "http.response.status_code": 200,
        "url.scheme": "https",
        "apitally.consumer.identifier": "tenant-1",
    }
    assert 0 < duration_point.start_time_unix_nano < duration_point.time_unix_nano

    def expected_point(**fields) -> ExponentialHistogramDataPoint:
        return ExponentialHistogramDataPoint(
            attributes=duration_point.attributes,
            start_time_unix_nano=duration_point.start_time_unix_nano,
            time_unix_nano=duration_point.time_unix_nano,
            scale=3,
            **fields,
        )

    # 0.1 maps through the logarithm and 0.125 through the exact power-of-two branch
    assert duration_point == expected_point(
        count=3,
        sum=0.1 + 0.125 + 0.125,
        min=0.1,
        max=0.125,
        positive=ExponentialHistogramDataPoint.Buckets(offset=-27, bucket_counts=[1, 0, 2]),
    )
    assert request_size_point == expected_point(count=3, sum=0, min=0, max=0, zero_count=3)
    # An exact power of two (1024) belongs to the bucket below its boundary, with 1000
    assert response_size_point == expected_point(
        count=3,
        sum=1000 + 1024 + 1025,
        min=1000,
        max=1025,
        positive=ExponentialHistogramDataPoint.Buckets(offset=79, bucket_counts=[2, 1]),
    )


def test_new_combinations_beyond_capacity_are_dropped_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr(metrics, "MAX_COMBINATIONS", 2)
    for _ in range(2):
        for route in ("/a", "/b", "/c", "/a"):
            metrics.record_request("GET", route, 200, consumer=None, duration=0.1)
        points = duration_data_points()
        assert {point_attributes(point)["http.route"]: point.count for point in points} == {"/a": 2, "/b": 1}
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "some request metrics are missing" in warnings[0].getMessage()


def test_each_collection_contains_only_combinations_recorded_since_previous():
    metrics.record_request("GET", "/a", 200, consumer=None, duration=0.1)
    (first_point,) = duration_data_points()
    metrics.record_request("GET", "/b", 200, consumer=None, duration=0.1)
    (second_point,) = duration_data_points()
    assert point_attributes(second_point)["http.route"] == "/b"
    assert second_point.start_time_unix_nano == first_point.time_unix_nano


def test_split_requests_keep_combination_histograms_together(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(metrics, "COMBINATIONS_PER_REQUEST", 2)
    for route in ("/a", "/b", "/c"):
        metrics.record_request("GET", route, 200, consumer=None, duration=0.1, request_size=10, response_size=100)
    _, *histogram_entries = collect_metrics().resource_metrics
    routes_by_entry = [
        {
            metric.name: [point_attributes(point)["http.route"] for point in metric.exponential_histogram.data_points]
            for scope_metrics in entry.scope_metrics
            for metric in scope_metrics.metrics
        }
        for entry in histogram_entries
    ]
    assert routes_by_entry == [
        dict.fromkeys(HISTOGRAM_NAMES, ["/a", "/b"]),
        dict.fromkeys(HISTOGRAM_NAMES, ["/c"]),
    ]


def test_collection_pauses_between_slices_but_not_after_last(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(metrics, "COMBINATIONS_PER_REQUEST", 1)
    for route in ("/a", "/b", "/c"):
        metrics.record_request("GET", route, 200, consumer=None, duration=0.1)
    stop_event = threading.Event()
    pauses: list[float | None] = []
    monkeypatch.setattr(stop_event, "wait", lambda timeout=None: pauses.append(timeout) or False)
    _, *histogram_entries = collect_metrics(stop_event).resource_metrics
    assert len(histogram_entries) == 3
    assert pauses == [metrics.SLICE_PAUSE_SECONDS] * 2


def test_collection_writes_all_slices_when_stop_event_is_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(metrics, "COMBINATIONS_PER_REQUEST", 1)
    for route in ("/a", "/b", "/c"):
        metrics.record_request("GET", route, 200, consumer=None, duration=0.1)
    stop_event = threading.Event()
    stop_event.set()
    _, *histogram_entries = collect_metrics(stop_event).resource_metrics
    assert len(histogram_entries) == 3


def test_process_gauges_exported_without_traffic():
    (entry,) = collect_metrics().resource_metrics
    (scope_metrics,) = entry.scope_metrics
    assert {
        metric.name: (metric.unit, metric.WhichOneof("data"), metric.gauge.data_points[0].WhichOneof("value"))
        for metric in scope_metrics.metrics
    } == {
        "process.uptime": ("s", "gauge", "as_double"),
        "process.cpu.utilization": ("1", "gauge", "as_double"),
        "process.memory.usage": ("By", "gauge", "as_int"),
    }
    points = [point for metric in scope_metrics.metrics for point in metric.gauge.data_points]
    assert len(points) == 3
    assert all(not point.attributes for point in points)
    assert len({point.time_unix_nano for point in points}) == 1


def test_options_and_unmatched_route_not_recorded():
    metrics.record_request("OPTIONS", "/items", 204, consumer=None, duration=0.01)
    metrics.record_request("GET", "", 404, consumer=None, duration=0.01)
    assert duration_data_points() == []


def test_unknown_sizes_skip_size_observations():
    metrics.record_request("GET", "/a", 500, consumer=None, duration=0.01, scheme="http")
    data_points = collect_data_points()
    (point,) = data_points["http.server.request.duration"]
    assert point_attributes(point) == {
        "http.request.method": "GET",
        "http.route": "/a",
        "http.response.status_code": 500,
        "url.scheme": "http",
        "error.type": "500",
    }
    assert "http.server.request.body.size" not in data_points
    assert "http.server.response.body.size" not in data_points
