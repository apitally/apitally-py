from __future__ import annotations

import sys
import threading
from collections.abc import Callable, MutableMapping
from typing import TYPE_CHECKING, Any

import pytest

from apitally.shared import activation, log_processor, metrics, providers
from apitally.shared.asgi import Message, Receive, Scope, Send
from tests.conftest import WRITE_TOKEN, InMemoryExporters


if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIEnvironment


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
