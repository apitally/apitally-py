from __future__ import annotations

from collections.abc import Iterator

import pytest
from django.test import Client

from tests.conftest import InMemoryExporters
from tests.django_utils import (
    activate_via_signal,
    attach_metric_reader,
    configure_django_settings,
    get_histogram_points,
    get_server_spans,
    get_startup_payload,
    init,
    reset_django_settings,
    teardown_django_instrumentation,
)


@pytest.fixture(scope="module", autouse=True)
def django_settings() -> Iterator[None]:
    configure_django_settings(ROOT_URLCONF="tests.django_rest_framework_urls")
    yield
    reset_django_settings()


@pytest.fixture(autouse=True)
def django_teardown() -> Iterator[None]:
    yield
    teardown_django_instrumentation()


def test_startup_paths_include_viewset_route_templates(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    init(monkeypatch)
    activate_via_signal()

    payload = get_startup_payload(exporters)
    assert payload["versions"]["djangorestframework"]
    assert {"method": "GET", "path": "/items/"} in payload["paths"]
    assert {"method": "GET", "path": "/items/{pk}/"} in payload["paths"]


def test_request_flow(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    init(monkeypatch)
    activate_via_signal()
    reader = attach_metric_reader()

    response = Client().get("/items/42/")
    assert response.status_code == 200

    (span,) = get_server_spans(exporters)
    assert span.attributes is not None
    assert span.attributes["http.route"] == "/items/{pk}/"
    assert span.attributes["http.response.status_code"] == 200
    (point,) = get_histogram_points(reader, "http.server.request.duration")
    assert point.attributes["http.route"] == "/items/{pk}/"
