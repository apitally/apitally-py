import json
import platform

import pytest
from opentelemetry.sdk._logs import ReadableLogRecord

from apitally.shared import activation, startup
from tests.conftest import WRITE_TOKEN, InMemoryExporters


PATHS = [{"method": "GET", "path": "/users"}, {"method": "POST", "path": "/users"}]


def activate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    activation.configure(write_token=WRITE_TOKEN)
    activation.activate()
    assert activation.is_activated()


def startup_records(exporters: InMemoryExporters) -> list[ReadableLogRecord]:
    if activation.log_processor is not None:
        activation.log_processor.force_flush()
    return [
        exported
        for exporter in exporters.log
        for exported in exporter.get_finished_logs()
        if exported.log_record.event_name == startup.EVENT_NAME
    ]


def test_startup_event_record_and_payload(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    startup.set_app_info(
        framework="fastapi",
        paths=lambda: PATHS,
        versions={"fastapi": "0.115.0", "app": "2.3.1"},
        openapi='{"openapi": "3.1.0"}',
    )
    activate(monkeypatch)

    (exported,) = startup_records(exporters)
    record = exported.log_record
    assert exported.instrumentation_scope is not None
    assert exported.instrumentation_scope.name == "apitally"
    assert record.event_name == "apitally.app.startup"
    assert record.timestamp is not None
    assert record.trace_id == 0
    assert isinstance(record.body, str)
    assert json.loads(record.body) == {
        "framework": "fastapi",
        "versions": {"python": platform.python_version(), "fastapi": "0.115.0", "app": "2.3.1"},
        "paths": PATHS,
        "openapi": '{"openapi": "3.1.0"}',
    }


def test_openapi_over_4mb_omitted_paths_remain(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    openapi = '{"openapi": "3.1.0", "padding": "' + "x" * 4_000_000 + '"}'
    startup.set_app_info(framework="fastapi", paths=PATHS, openapi=openapi)
    activate(monkeypatch)

    (exported,) = startup_records(exporters)
    assert isinstance(exported.log_record.body, str)
    payload = json.loads(exported.log_record.body)
    assert "openapi" not in payload
    assert payload["paths"] == PATHS


def test_emitted_once_across_activation_lifecycle(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    startup.set_app_info(framework="flask", paths=PATHS)
    activate(monkeypatch)

    # Ignored re-call (adapter re-init) and simulated after-fork-in-parent re-activation
    activation.configure(write_token=WRITE_TOKEN, env="dev")
    startup.set_app_info(framework="flask", paths=PATHS)
    activation.activate()
    activation.before_fork()
    activation.after_fork_in_parent()

    assert len(startup_records(exporters)) == 1
