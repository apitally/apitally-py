import json
import platform

from apitally.shared import activation, startup


TOKEN = "apt_" + "a" * 24
PATHS = [{"method": "GET", "path": "/users"}, {"method": "POST", "path": "/users"}]


def activate(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    activation.configure(write_token=TOKEN)
    activation.activate()
    assert activation.is_activated()


def startup_records(memory_exporters):
    if activation.log_processor is not None:
        activation.log_processor.force_flush()
    return [
        exported
        for exporter in memory_exporters.log
        for exported in exporter.get_finished_logs()
        if exported.log_record.event_name == startup.EVENT_NAME
    ]


def test_startup_event_record_and_payload(memory_exporters, monkeypatch):
    startup.set_app_info(
        framework="fastapi",
        paths=lambda: PATHS,
        versions={"fastapi": "0.115.0", "app": "2.3.1"},
        openapi='{"openapi": "3.1.0"}',
    )
    activate(monkeypatch)

    (exported,) = startup_records(memory_exporters)
    record = exported.log_record
    assert exported.instrumentation_scope is not None
    assert exported.instrumentation_scope.name == "apitally"
    assert record.event_name == "apitally.app.startup"
    assert record.timestamp is not None
    assert record.trace_id == 0
    assert json.loads(record.body) == {
        "framework": "fastapi",
        "versions": {"python": platform.python_version(), "fastapi": "0.115.0", "app": "2.3.1"},
        "paths": PATHS,
        "openapi": '{"openapi": "3.1.0"}',
    }


def test_openapi_over_4mb_omitted_paths_remain(memory_exporters, monkeypatch):
    openapi = '{"openapi": "3.1.0", "padding": "' + "x" * 4_000_000 + '"}'
    startup.set_app_info(framework="fastapi", paths=PATHS, openapi=openapi)
    activate(monkeypatch)

    (exported,) = startup_records(memory_exporters)
    payload = json.loads(exported.log_record.body)
    assert "openapi" not in payload
    assert payload["paths"] == PATHS


def test_emitted_once_across_activation_lifecycle(memory_exporters, monkeypatch):
    startup.set_app_info(framework="flask", paths=PATHS)
    activate(monkeypatch)

    # Ignored re-call (adapter re-init) and simulated after-fork-in-parent re-activation
    activation.configure(write_token=TOKEN, env="dev")
    startup.set_app_info(framework="flask", paths=PATHS)
    activation.activate()
    activation.before_fork()
    activation.after_fork_in_parent()

    assert len(startup_records(memory_exporters)) == 1
