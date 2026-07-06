from __future__ import annotations

import sys
from typing import Any

import pytest

from apitally.django import init_apitally
from apitally.shared import activation


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


def teardown_django_instrumentation() -> None:
    from django.core.signals import request_started
    from opentelemetry.instrumentation.django import DjangoInstrumentor

    instrumentor = DjangoInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    request_started.disconnect(dispatch_uid="apitally")
