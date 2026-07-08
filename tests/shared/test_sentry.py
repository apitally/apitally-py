import sys
from collections.abc import Generator
from typing import TYPE_CHECKING

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Tracer

from apitally.shared import activation, sentry
from apitally.shared.span_processor import ApitallySpanProcessor
from tests.conftest import WRITE_TOKEN


if TYPE_CHECKING:
    from sentry_sdk.envelope import Envelope


sentry_sdk = pytest.importorskip("sentry_sdk")
sentry_scope = pytest.importorskip("sentry_sdk.scope")
sentry_transport = pytest.importorskip("sentry_sdk.transport")


class DiscardTransport(sentry_transport.Transport):
    def capture_envelope(self, envelope: "Envelope") -> None:
        pass


@pytest.fixture(autouse=True)
def reset_sentry_state() -> Generator[None]:
    yield
    sentry_scope.global_event_processors[:] = [
        p for p in sentry_scope.global_event_processors if p is not sentry.sentry_event_processor
    ]
    sentry.installed = False


@pytest.fixture()
def sentry_initialized() -> Generator[None]:
    sentry_sdk.init(
        dsn="https://1234567890@example.invalid/1",
        transport=DiscardTransport(),
        default_integrations=False,
    )
    yield
    sentry_sdk.get_client().close()


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture()
def tracer(exporter: InMemorySpanExporter) -> Tracer:
    provider = TracerProvider()
    provider.add_span_processor(ApitallySpanProcessor(SimpleSpanProcessor(exporter)))
    return provider.get_tracer("test")


def capture_exception_in_server_span(tracer: Tracer) -> str | None:
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        try:
            raise ValueError("boom")
        except ValueError as exc:
            return sentry_sdk.capture_exception(exc)


def test_sentry_event_id_written_to_server_span(
    sentry_initialized: None, tracer: Tracer, exporter: InMemorySpanExporter
):
    activation.configure(write_token=WRITE_TOKEN)
    activation.configure(write_token=WRITE_TOKEN)
    assert sentry_scope.global_event_processors.count(sentry.sentry_event_processor) == 1

    event_id = capture_exception_in_server_span(tracer)

    assert event_id is not None
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes is not None
    assert spans[0].attributes["apitally.exception.sentry_event_id"] == event_id


def test_configure_without_sentry_sdk_installs_nothing(monkeypatch: pytest.MonkeyPatch):
    processors_before = list(sentry_scope.global_event_processors)
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)
    monkeypatch.setitem(sys.modules, "sentry_sdk.scope", None)

    activation.configure(write_token=WRITE_TOKEN)

    assert not sentry.installed
    assert sentry_scope.global_event_processors == processors_before
