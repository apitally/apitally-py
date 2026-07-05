import multiprocessing
import os
import sys
import threading
import time

import pytest
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from apitally.shared import activation, metrics
from apitally.shared.span_processor import ApitallySpanProcessor


TOKEN = "apt_" + "a" * 24

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="real-fork tests run on Linux CI only")


def configure_and_activate(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    activation.configure(write_token=TOKEN)
    activation.activate()
    assert activation.is_activated()


def test_before_fork_quiesces_sdk_threads(memory_exporters, monkeypatch):
    threads_before = set(threading.enumerate())
    configure_and_activate(monkeypatch)
    started = [t for t in threading.enumerate() if t not in threads_before]
    assert started

    activation.before_fork()
    assert not any(t.is_alive() for t in started)
    assert metrics.reader is None


def test_after_fork_in_parent_reactivates_pipelines(memory_exporters, monkeypatch):
    configure_and_activate(monkeypatch)
    assert len(memory_exporters.span) == 1
    old_reader = metrics.reader

    activation.before_fork()
    activation.after_fork_in_parent()

    assert len(memory_exporters.span) == 2
    assert metrics.reader is not None and metrics.reader is not old_reader

    with trace.get_tracer("test").start_as_current_span("GET /items", kind=SpanKind.SERVER):
        pass
    assert activation.span_processor is not None
    assert activation.span_processor.downstream.force_flush()
    assert len(memory_exporters.span[1].get_finished_spans()) == 1
    assert memory_exporters.span[0].get_finished_spans() == ()


def test_after_fork_in_child_leaves_fresh_acquirable_lock(memory_exporters, monkeypatch):
    configure_and_activate(monkeypatch)
    activation.before_fork()  # holds the lock, as at the instant of fork

    activation.after_fork_in_child()

    assert activation.activation_lock.acquire(blocking=False)
    activation.activation_lock.release()


def test_child_reactivation_reuses_inherited_span_processor(memory_exporters, monkeypatch):
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
    assert len(memory_exporters.span[-1].get_finished_spans()) == 1


def child_probe(queue):
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
def test_forked_child_stays_inert_until_activation_gate(memory_exporters, monkeypatch):
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
def test_os_fork_in_activated_process_does_not_deadlock(memory_exporters, monkeypatch):
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
