from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from apitally.shared import activation, sentry
from apitally.shared.exporter import ApitallySpanExporter
from apitally.shared.span_processor import ApitallySpanProcessor
from tests.conftest import CONTRIB_SCOPE, WRITE_TOKEN


if TYPE_CHECKING:
    from sentry_sdk.envelope import Envelope


sentry_sdk = pytest.importorskip("sentry_sdk")
sentry_scope = pytest.importorskip("sentry_sdk.scope")
sentry_transport = pytest.importorskip("sentry_sdk.transport")


class DiscardTransport(sentry_transport.Transport):
    def capture_envelope(self, envelope: Envelope) -> None:
        pass


@pytest.fixture(autouse=True)
def reset_sentry_state() -> Generator[None]:
    yield
    sentry_scope.global_event_processors[:] = [
        p for p in sentry_scope.global_event_processors if p is not sentry.sentry_event_processor
    ]
    sentry.installed = False
    sentry.pending_event_ids.clear()


@pytest.fixture(autouse=True)
def initialize_sentry() -> Generator[None]:
    sentry_sdk.init(
        dsn="https://1234567890@example.invalid/1",
        transport=DiscardTransport(),
        default_integrations=False,
    )
    yield
    sentry_sdk.get_client().close()


def test_sentry_event_id_written_after_server_span_ended():
    activation.configure(write_token=WRITE_TOKEN)
    activation.configure(write_token=WRITE_TOKEN)
    assert sentry_scope.global_event_processors.count(sentry.sentry_event_processor) == 1

    span_exporter = InMemorySpanExporter()
    batch_processor = BatchSpanProcessor(ApitallySpanExporter(span_exporter), schedule_delay_millis=60_000)
    provider = TracerProvider()
    provider.add_span_processor(ApitallySpanProcessor(batch_processor))
    tracer = provider.get_tracer(CONTRIB_SCOPE)
    try:
        error: ValueError | None = None
        with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as span:
            try:
                raise ValueError("boom")
            except ValueError as exc:
                span.record_exception(exc)
                error = exc

        # Mimics Sentry's outermost ASGI wrapper capturing after the SERVER span has ended
        event_id = sentry_sdk.capture_exception(error)
        batch_processor.force_flush()
    finally:
        provider.shutdown()

    assert event_id is not None
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes is not None
    assert spans[0].attributes[sentry.SENTRY_EVENT_ID_ATTRIBUTE] == event_id
