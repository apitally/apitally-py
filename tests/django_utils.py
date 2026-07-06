from __future__ import annotations

import json
import sys
from typing import Any

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind

from apitally.django import init_apitally
from apitally.shared import activation, metrics, startup


TOKEN = "apt_" + "a" * 24


def configure_django_settings(**settings_kwargs: Any) -> None:
    import django
    from django.conf import settings
    from django.utils.functional import empty

    settings._wrapped = empty
    settings.configure(
        ALLOWED_HOSTS=["testserver"],
        SECRET_KEY="secret",
        DEBUG=False,
        MIDDLEWARE=[],
        # Identical app list in every module: apps.populate runs once per process (DRF needs auth)
        INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth"],
        **settings_kwargs,
    )
    django.setup()


def reset_django_settings() -> None:
    from django.conf import settings
    from django.utils.functional import empty

    settings._wrapped = empty


def init(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sys, "argv", ["manage.py", "runserver"])
    init_apitally(write_token=TOKEN, **kwargs)


def activate_via_signal() -> None:
    from django.core.signals import request_started

    request_started.send(sender=None)
    assert activation.is_activated()


def attach_metric_reader() -> InMemoryMetricReader:
    reader = InMemoryMetricReader(
        preferred_temporality=metrics.HISTOGRAM_PREFERRED_TEMPORALITY,
        preferred_aggregation=metrics.HISTOGRAM_PREFERRED_AGGREGATION,
    )
    assert metrics.meter_provider is not None
    metrics.meter_provider.add_metric_reader(reader)
    return reader


def get_server_spans(exporters: Any) -> list[ReadableSpan]:
    assert activation.span_processor is not None
    activation.span_processor.force_flush()
    return [
        span for exporter in exporters.span for span in exporter.get_finished_spans() if span.kind == SpanKind.SERVER
    ]


def get_startup_payload(exporters: Any) -> dict[str, Any]:
    assert activation.log_processor is not None
    activation.log_processor.force_flush()
    (record,) = [
        exported.log_record
        for exporter in exporters.log
        for exported in exporter.get_finished_logs()
        if exported.log_record.event_name == startup.EVENT_NAME
    ]
    assert isinstance(record.body, str)
    return json.loads(record.body)


def get_histogram_points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    metrics_data = reader.get_metrics_data()
    if metrics_data is None:
        return []
    return [
        point
        for resource_metrics in metrics_data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def teardown_django_instrumentation() -> None:
    from django.core.signals import request_started
    from opentelemetry.instrumentation.django import DjangoInstrumentor

    instrumentor = DjangoInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    request_started.disconnect(dispatch_uid="apitally")
