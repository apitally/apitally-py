import json
from collections.abc import Iterator

import pytest
from django.test import Client
from opentelemetry.trace import SpanKind

from tests.conftest import (
    InMemoryExporters,
    attach_metric_reader,
    duration_data_points,
    exported_spans,
    startup_payload,
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

    payload = startup_payload(exporters)
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

    (span,) = exported_spans(exporters, kind=SpanKind.SERVER)
    assert span.attributes is not None
    assert span.attributes["http.route"] == "/api/foo/{bar}"
    assert span.attributes["http.response.status_code"] == 200
    (point,) = duration_data_points(reader)
    assert (point.attributes or {})["http.route"] == "/api/foo/{bar}"
