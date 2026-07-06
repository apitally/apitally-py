from __future__ import annotations

import json
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
    configure_django_settings(ROOT_URLCONF="tests.django_ninja_urls")
    yield
    reset_django_settings()


@pytest.fixture(autouse=True)
def django_teardown() -> Iterator[None]:
    yield
    teardown_django_instrumentation()


def test_startup_paths_and_openapi(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    init(monkeypatch, app_version="1.2.3")
    activate_via_signal()

    payload = get_startup_payload(exporters)
    assert payload["framework"] == "django"
    assert payload["versions"]["django-ninja"]
    assert payload["versions"]["app"] == "1.2.3"
    assert {(p["method"], p["path"]) for p in payload["paths"]} == {("GET", "/api/foo"), ("GET", "/api/foo/{bar}")}
    assert any(p["path"] == "/api/foo" and p.get("summary") == "Foo" for p in payload["paths"])
    openapi = json.loads(payload["openapi"])
    assert set(openapi["paths"]) == {"/api/foo", "/api/foo/{bar}"}


def test_request_flow(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    init(monkeypatch)
    activate_via_signal()
    reader = attach_metric_reader()

    response = Client().get("/api/foo/123")
    assert response.status_code == 200

    (span,) = get_server_spans(exporters)
    assert span.attributes is not None
    assert span.attributes["http.route"] == "/api/foo/{bar}"
    assert span.attributes["http.response.status_code"] == 200
    (point,) = get_histogram_points(reader, "http.server.request.duration")
    assert point.attributes["http.route"] == "/api/foo/{bar}"
