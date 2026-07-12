from __future__ import annotations

import multiprocessing
import os
import sys
import threading
import time
from collections.abc import Callable, MutableMapping
from typing import TYPE_CHECKING, Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from apitally.shared import activation, log_processor, metrics, providers
from apitally.shared.asgi import Message, Receive, Scope, Send
from apitally.shared.span_processor import ApitallySpanProcessor
from tests.conftest import WRITE_TOKEN, InMemoryExporters, exported_spans


if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIEnvironment


linux_only = pytest.mark.skipif(sys.platform != "linux", reason="real-fork tests run on Linux CI only")


def configure_and_activate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    activation.configure(write_token=WRITE_TOKEN)
    activation.activate()
    assert activation.is_activated()


async def drive_lifespan(shim: activation.ASGIActivationShim) -> None:
    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: MutableMapping[str, Any]) -> None:
        pass

    await shim({"type": "lifespan"}, receive, send)


def test_configure_starts_no_threads():
    threads_before = set(threading.enumerate())
    activation.configure(write_token=WRITE_TOKEN)
    assert set(threading.enumerate()) == threads_before
    assert not activation.is_activated()


async def test_asgi_shim_activates_once_on_lifespan_startup_complete(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    activation.configure(write_token=WRITE_TOKEN)
    hook_calls = []
    activation.register_on_activate_hook(lambda: hook_calls.append(1))

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send({"type": "lifespan.startup.complete"})

    shim = activation.ASGIActivationShim(app)
    await drive_lifespan(shim)
    await drive_lifespan(shim)

    assert activation.is_activated()
    assert hook_calls == [1]


async def test_asgi_shim_startup_failed_defers_to_first_request(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    activation.configure(write_token=WRITE_TOKEN)
    activated_during_request = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await receive()
            await send({"type": "lifespan.startup.failed"})
        else:
            activated_during_request.append(activation.is_activated())

    shim = activation.ASGIActivationShim(app)
    await drive_lifespan(shim)
    assert not activation.is_activated()

    async def receive() -> Message:
        return {"type": "http.request"}

    async def send(message: MutableMapping[str, Any]) -> None:
        pass

    await shim({"type": "http"}, receive, send)
    assert activated_during_request == [True]


def test_wsgi_shim_activates_before_first_request_proceeds(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    activation.configure(write_token=WRITE_TOKEN)
    activated_during_request = []

    def wsgi_app(environ: WSGIEnvironment, start_response: StartResponse) -> list[bytes]:
        activated_during_request.append(activation.is_activated())
        start_response("200 OK", [])
        return [b"ok"]

    def start_response(
        status: str, headers: list[tuple[str, str]], exc_info: object = None
    ) -> Callable[[bytes], object]:
        return lambda data: None

    shim = activation.WSGIActivationShim(wsgi_app)
    assert list(shim({}, start_response)) == [b"ok"]
    assert activated_during_request == [True]


@pytest.mark.parametrize("guard", ["pytest_env", "manage_py_test", "disabled_env", "disabled_kwarg"])
def test_test_environment_guard_skips_activation(monkeypatch: pytest.MonkeyPatch, guard: str):
    exporter_calls = []
    monkeypatch.setattr(providers, "create_span_exporter", lambda env: exporter_calls.append("span"))
    monkeypatch.setattr(providers, "create_log_exporter", lambda env: exporter_calls.append("log"))
    monkeypatch.setattr(metrics, "create_metric_exporter", lambda env, **kwargs: exporter_calls.append("metric"))

    activation.configure(write_token=WRITE_TOKEN, disabled=(guard == "disabled_kwarg"))
    if guard != "pytest_env":
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    if guard == "manage_py_test":
        monkeypatch.setattr(sys, "argv", ["manage.py", "test"])
    if guard == "disabled_env":
        monkeypatch.setenv("APITALLY_DISABLED", "1")

    activation.activate()
    assert not activation.is_activated()
    assert exporter_calls == []


def test_on_activate_hooks_run_last(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    observed = []
    activation.register_on_activate_hook(
        lambda: observed.append(
            (activation.is_activated(), metrics.reader is not None, log_processor.installed_handler is not None)
        )
    )
    activation.configure(write_token=WRITE_TOKEN)
    activation.activate()
    assert observed == [(True, True, True)]


def test_activation_attaches_to_existing_user_tracer_provider(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    user_exporter = InMemorySpanExporter()
    user_provider = TracerProvider(resource=Resource.create({"deployment.environment.name": "production"}))
    user_provider.add_span_processor(SimpleSpanProcessor(user_exporter))
    trace.set_tracer_provider(user_provider)

    activation.configure(write_token=WRITE_TOKEN)
    activation.activate()
    assert trace.get_tracer_provider() is user_provider
    assert activation.env == "production"

    with trace.get_tracer("test").start_as_current_span("GET /items", kind=SpanKind.SERVER):
        pass
    assert len(user_exporter.get_finished_spans()) == 1
    (span,) = exported_spans(exporters)
    assert span.name == "GET /items"


def test_before_fork_stops_sdk_threads(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    threads_before = set(threading.enumerate())
    configure_and_activate(monkeypatch)
    started = [t for t in threading.enumerate() if t not in threads_before]
    assert started

    activation.before_fork()
    assert not any(t.is_alive() for t in started)
    assert metrics.reader is None


def test_after_fork_in_parent_reactivates_pipelines(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    configure_and_activate(monkeypatch)
    assert len(exporters.span) == 1
    old_reader = metrics.reader

    activation.before_fork()
    activation.after_fork_in_parent()

    assert len(exporters.span) == 2
    assert metrics.reader is not None and metrics.reader is not old_reader

    with trace.get_tracer("test").start_as_current_span("GET /items", kind=SpanKind.SERVER):
        pass
    assert activation.span_processor is not None
    assert activation.span_processor.downstream.force_flush()
    assert len(exporters.span[1].get_finished_spans()) == 1
    assert exporters.span[0].get_finished_spans() == ()


def test_after_fork_in_child_leaves_fresh_acquirable_lock(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    configure_and_activate(monkeypatch)
    activation.before_fork()  # holds the lock, as at the instant of fork

    activation.after_fork_in_child()

    assert activation.activation_lock.acquire(blocking=False)
    activation.activation_lock.release()


def test_child_reactivation_reuses_inherited_span_processor(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    configure_and_activate(monkeypatch)
    provider = trace.get_tracer_provider()

    activation.before_fork()
    activation.after_fork_in_child()
    activation.activate()

    assert activation.is_activated()
    processors = provider._active_span_processor._span_processors  # ty: ignore[unresolved-attribute]
    assert sum(1 for p in processors if isinstance(p, ApitallySpanProcessor)) == 1
    with trace.get_tracer("test").start_as_current_span("GET /items", kind=SpanKind.SERVER):
        pass
    assert activation.span_processor is not None
    assert activation.span_processor.downstream.force_flush()
    assert len(exporters.span[-1].get_finished_spans()) == 1


def test_child_reactivation_clears_inherited_request_state(
    exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch
):
    configure_and_activate(monkeypatch)
    span = trace.get_tracer("test").start_span("GET /items", kind=SpanKind.SERVER)  # in flight at fork

    activation.before_fork()
    activation.after_fork_in_child()
    activation.activate()

    assert activation.span_processor is not None
    assert not activation.span_processor.spans
    assert not activation.span_processor.pending
    span.end()
    assert exporters.span[-1].get_finished_spans() == ()


def child_probe(queue: multiprocessing.Queue[dict[str, Any]]) -> None:
    inert_after_fork = not activation.is_activated()
    activation.activate()
    resource = activation.resource
    queue.put(
        {
            "inert_after_fork": inert_after_fork,
            "activated_after_gate": activation.is_activated(),
            "instance_id": resource.attributes["service.instance.id"] if resource is not None else None,
        }
    )


@linux_only
def test_forked_child_stays_inert_until_activation_gate(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    configure_and_activate(monkeypatch)
    assert activation.resource is not None
    parent_instance_id = activation.resource.attributes["service.instance.id"]

    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    process = ctx.Process(target=child_probe, args=(queue,))
    process.start()
    result = queue.get(timeout=15)
    process.join(15)

    assert process.exitcode == 0
    assert result["inert_after_fork"] is True
    assert result["activated_after_gate"] is True
    assert result["instance_id"] is not None
    assert result["instance_id"] != parent_instance_id


@linux_only
def test_os_fork_in_activated_process_does_not_deadlock(exporters: InMemoryExporters, monkeypatch: pytest.MonkeyPatch):
    configure_and_activate(monkeypatch)
    pid = os.fork()
    if pid == 0:
        # Child: the after-fork handler must have reset to configured state
        os._exit(0 if not activation.is_activated() else 1)

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        done_pid, status = os.waitpid(pid, os.WNOHANG)
        if done_pid:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        raise AssertionError("Forked child did not exit, possible deadlock")

    assert os.WEXITSTATUS(status) == 0
    assert activation.is_activated()
    assert metrics.reader is not None
