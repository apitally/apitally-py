import json
import sys
from collections.abc import Iterator

import pytest
from django.conf import settings
from django.test import Client
from django.utils.functional import empty, lazy
from opentelemetry.trace import SpanKind

from apitally.django import APITALLY_MIDDLEWARE, OTEL_MIDDLEWARE, _convert_proxy_objects, init_apitally
from apitally.shared import activation, config
from apitally.shared.redaction import REDACTED
from tests.conftest import (
    InMemoryExporters,
    attach_metric_reader,
    duration_data_points,
    exported_spans,
    metric_data_points,
    startup_payload,
    unwrap,
)
from tests.django_utils import (
    activate_via_signal,
    configure_django_settings,
    init,
    reset_django_settings,
    teardown_django_instrumentation,
)


@pytest.fixture(scope="module", autouse=True)
def django_settings() -> Iterator[None]:
    configure_django_settings(ROOT_URLCONF="tests.django_urls")
    yield
    reset_django_settings()


@pytest.fixture(autouse=True)
def django_teardown() -> Iterator[None]:
    yield
    teardown_django_instrumentation()


def test_request_span_and_activation_order(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    init(monkeypatch)
    assert not activation.is_activated()

    response = Client().get("/items/123/")
    assert response.status_code == 200
    assert activation.is_activated()

    # request_started activated before the span started, so the very first request is exported
    (span,) = exported_spans(exporters, kind=SpanKind.SERVER)
    assert span.attributes is not None
    assert span.attributes["http.route"] == "/items/{pk}/"
    assert span.attributes["http.response.status_code"] == 200
    payload = startup_payload(exporters)
    assert payload["framework"] == "django"
    assert payload["versions"]["django"]


def test_management_command_configures_but_never_activates(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sys, "argv", ["manage.py", "migrate"])
    init_apitally(write_token="apt_" + "a" * 24)
    assert config.get_config() is not None
    assert not activation.is_activated()
    assert exporters.span == []


def test_bodies_and_request_headers_captured_and_redacted(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(
        monkeypatch,
        log_request_body=True,
        log_response_body=True,
        log_request_headers=True,
        mask_body_fields=["custom_field"],
    )
    response = Client().post(
        "/items/",
        data={"name": "a", "password": "hunter2", "custom_field": "x"},
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer secret",
    )
    assert response.status_code == 201

    (span,) = exported_spans(exporters, kind=SpanKind.SERVER)
    assert span.attributes is not None
    redacted = {"name": "a", "password": REDACTED, "custom_field": REDACTED}
    assert json.loads(str(span.attributes["apitally.request.body"])) == redacted
    assert json.loads(str(span.attributes["apitally.response.body"])) == redacted
    assert span.attributes["http.request.header.authorization"] == [REDACTED]
    assert span.attributes["http.request.header.content-type"] == ("application/json",)


def test_sampled_out_request_skips_capture(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    mask_calls: list[bytes] = []

    def mask(span: object, body: bytes) -> bytes:
        mask_calls.append(body)
        return body

    init(
        monkeypatch,
        sample_rate=0.0,
        log_request_body=True,
        log_response_body=True,
        mask_request_body=mask,
        mask_response_body=mask,
    )
    activate_via_signal()
    reader = attach_metric_reader()

    response = Client().post("/items/", data={"name": "a"}, content_type="application/json")
    assert response.status_code == 201

    assert not mask_calls
    assert exported_spans(exporters, kind=SpanKind.SERVER) == []
    (point,) = duration_data_points(reader)
    assert point.count == 1


def test_streaming_response_recorded_without_body_and_size(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(monkeypatch, log_response_body=True)
    activate_via_signal()
    reader = attach_metric_reader()

    response = Client().get("/stream/")
    assert b"".join(response.streaming_content) == b"chunk1chunk2"  # ty: ignore[unresolved-attribute]

    (span,) = exported_spans(exporters, kind=SpanKind.SERVER)
    assert span.attributes is not None
    assert "apitally.response.body" not in span.attributes
    assert "http.response.body.size" not in span.attributes
    (point,) = duration_data_points(reader)
    assert (point.attributes or {})["http.route"] == "/stream/"
    assert metric_data_points(reader, "http.server.response.body.size") == []


def test_nested_urlconf_route_includes_prefix(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    init(monkeypatch)
    activate_via_signal()
    reader = attach_metric_reader()

    assert Client().get("/api/things/7/").status_code == 200

    (span,) = exported_spans(exporters, kind=SpanKind.SERVER)
    (point,) = duration_data_points(reader)
    assert unwrap(span.attributes)["http.route"] == "/api/things/{pk}/"
    assert unwrap(point.attributes)["http.route"] == "/api/things/{pk}/"


def test_consumer_reaches_span_and_histogram(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    init(monkeypatch)
    activate_via_signal()
    reader = attach_metric_reader()

    assert Client().get("/whoami/").status_code == 200

    (span,) = exported_spans(exporters, kind=SpanKind.SERVER)
    assert span.attributes is not None
    assert span.attributes["apitally.consumer.identifier"] == "tester"
    assert span.attributes["apitally.consumer.name"] == "Tester"
    (point,) = duration_data_points(reader)
    assert (point.attributes or {})["apitally.consumer.identifier"] == "tester"


def test_unhandled_exception_recorded(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    init(monkeypatch)
    activate_via_signal()
    reader = attach_metric_reader()

    response = Client(raise_request_exception=False).get("/error/")
    assert response.status_code == 500

    (span,) = exported_spans(exporters, kind=SpanKind.SERVER)
    assert span.attributes is not None
    assert span.attributes["http.response.status_code"] == 500
    (event,) = [e for e in span.events if e.name == "exception"]
    assert event.attributes is not None
    assert event.attributes["exception.type"] == "ValueError"
    (point,) = duration_data_points(reader)
    assert (point.attributes or {})["http.response.status_code"] == 500
    assert (point.attributes or {})["error.type"] == "500"


def test_include_django_views_adds_class_based_view_paths(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    init(monkeypatch, include_django_views=True)
    activate_via_signal()

    paths = startup_payload(exporters)["paths"]
    assert {"method": "GET", "path": "/notes/"} in paths
    assert {"method": "POST", "path": "/notes/"} in paths
    # Function-based views carry no method information, so they stay out
    assert not any(entry["path"] == "/whoami/" for entry in paths)


def test_lazy_schema_strings_converted_for_json():
    lazy_str = lazy(lambda: "Lazy", str)()
    converted = _convert_proxy_objects({"title": lazy_str, "tags": [lazy_str]})
    assert json.dumps(converted) == '{"title": "Lazy", "tags": ["Lazy"]}'


def test_init_from_settings_module(monkeypatch: pytest.MonkeyPatch):
    saved = settings._wrapped
    settings._wrapped = empty
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "tests.django_settings")
    sys.modules.pop("tests.django_settings", None)
    try:
        # Accessing settings imports the module, running init_apitally at its end
        assert settings.MIDDLEWARE == [
            OTEL_MIDDLEWARE,
            APITALLY_MIDDLEWARE,
            "django.middleware.common.CommonMiddleware",
        ]
    finally:
        settings._wrapped = saved
        sys.modules.pop("tests.django_settings", None)
