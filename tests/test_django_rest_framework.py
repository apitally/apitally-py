import json
from collections.abc import Iterator

import pytest
from django.test import Client, override_settings
from opentelemetry.trace import SpanKind

from tests.conftest import (
    InMemoryExporters,
    attach_metric_reader,
    duration_data_points,
    exported_spans,
    installed,
    startup_payload,
)
from tests.django.utils import (
    activate_via_signal,
    configure_django_settings,
    init,
    reset_django_settings,
    teardown_django_instrumentation,
)


@pytest.fixture(scope="module", autouse=True)
def django_settings() -> Iterator[None]:
    configure_django_settings(ROOT_URLCONF="tests.django.rest_framework_urls")
    yield
    reset_django_settings()


@pytest.fixture(autouse=True)
def django_teardown() -> Iterator[None]:
    yield
    teardown_django_instrumentation()


def test_startup_paths_include_viewset_route_templates(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    init(monkeypatch)
    activate_via_signal()

    payload = startup_payload(exporters)
    assert payload["versions"]["djangorestframework"]
    assert {"method": "GET", "path": "/items/"} in payload["paths"]
    assert {"method": "GET", "path": "/items/{pk}/"} in payload["paths"]


@pytest.mark.skipif(not installed("drf_spectacular"), reason="drf-spectacular is not installed")
def test_openapi_generated_via_drf_spectacular(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    with override_settings(REST_FRAMEWORK={"DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema"}):
        init(monkeypatch)
        activate_via_signal()
        payload = startup_payload(exporters)

    openapi = json.loads(payload["openapi"])
    assert openapi["openapi"].startswith("3.")
    assert "/items/{id}/" in openapi["paths"]


def test_request_flow(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    init(monkeypatch)
    activate_via_signal()
    reader = attach_metric_reader()

    response = Client().get("/items/42/")
    assert response.status_code == 200

    (span,) = exported_spans(exporters, kind=SpanKind.SERVER)
    assert span.attributes is not None
    assert span.attributes["http.route"] == "/items/{pk}/"
    assert span.attributes["http.response.status_code"] == 200
    (point,) = duration_data_points(reader)
    assert (point.attributes or {})["http.route"] == "/items/{pk}/"
