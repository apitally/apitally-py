from __future__ import annotations

import sys
from typing import Any

import django
import pytest
from django.conf import settings
from django.core.signals import request_started
from django.utils.functional import empty
from opentelemetry.instrumentation.django import DjangoInstrumentor

from apitally.django import init_apitally
from apitally.shared import activation
from tests.conftest import WRITE_TOKEN


def configure_django_settings(**settings_kwargs: Any) -> None:
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
    settings._wrapped = empty


def init(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sys, "argv", ["manage.py", "runserver"])
    init_apitally(write_token=WRITE_TOKEN, **kwargs)


def activate_via_signal() -> None:
    request_started.send(sender=None)
    assert activation.is_activated()


def teardown_django_instrumentation() -> None:
    instrumentor = DjangoInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    request_started.disconnect(dispatch_uid="apitally")
