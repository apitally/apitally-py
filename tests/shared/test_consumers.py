from contextvars import copy_context
from typing import Any

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Tracer

from apitally import capture_exception, set_consumer, set_request_attribute
from apitally.shared import consumers
from apitally.shared.consumers import emit_consumer_update_if_changed, get_consumer_identifier, init_consumer
from apitally.shared.context import get_server_span
from tests.conftest import consumer_update_bodies, unwrap


def test_set_consumer_targets_server_span_from_child_span(tracer: Tracer, span_exporter: InMemorySpanExporter):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        with tracer.start_as_current_span("child"):
            set_consumer(" acme-corp ", name=" Acme Corp ", group="enterprise")
    child, server = span_exporter.get_finished_spans()
    assert unwrap(server.attributes)["apitally.consumer.identifier"] == "acme-corp"
    assert "apitally.consumer.name" not in unwrap(server.attributes)
    assert "apitally.consumer.group" not in unwrap(server.attributes)
    assert not any(key.startswith("apitally.consumer.") for key in unwrap(child.attributes))
    assert get_consumer_identifier() == "acme-corp"


def test_consumer_set_in_copied_context_without_span_visible_from_parent_context():
    # Sync endpoints (anyio threadpool) and BaseHTTPMiddleware child tasks run in copied
    # contexts; the shared holder must carry the identifier back even with no recording span
    init_consumer()
    copy_context().run(set_consumer, "acme-corp")
    assert get_server_span() is None
    assert get_consumer_identifier() == "acme-corp"


def test_writes_to_excluded_request_stay_local(tracer: Tracer, span_exporter: InMemorySpanExporter):
    with tracer.start_as_current_span("GET /healthz", kind=SpanKind.SERVER, attributes={"url.path": "/healthz"}):
        set_consumer("acme-corp")
        set_request_attribute("tenant", "acme")
        capture_exception(ValueError("x"))
    assert span_exporter.get_finished_spans() == ()
    assert get_consumer_identifier() == "acme-corp"


def test_consumer_update_truncates_identifier_name_and_group(consumer_update_exporter: InMemoryLogRecordExporter):
    emit_update("i" * 200, name="n" * 100, group="g" * 100)
    assert consumer_update_bodies(consumer_update_exporter) == [
        {"identifier": "i" * 128, "name": "n" * 64, "group": "g" * 64}
    ]


def test_consumer_update_not_emitted_again_for_unchanged_payload(
    consumer_update_exporter: InMemoryLogRecordExporter,
):
    emit_update("acme-corp", name="Acme")
    emit_update("acme-corp", name="Acme")
    emit_update("acme-corp", name="Acme Corp")
    bodies = consumer_update_bodies(consumer_update_exporter)
    assert [body["name"] for body in bodies] == ["Acme", "Acme Corp"]


def test_no_consumer_update_without_metadata(consumer_update_exporter: InMemoryLogRecordExporter):
    emit_update("acme-corp")
    assert consumer_update_bodies(consumer_update_exporter) == []


def test_consumer_update_attributes_normalized(consumer_update_exporter: InMemoryLogRecordExporter):
    attributes: dict[str, Any] = {
        " plan ": " pro ",
        "seats": 5,
        "ratio": 0.5,
        "trial": False,
        "region": None,
        "tier": " ",
        "": "empty key",
        "k" * 65: "long key",
        "long": "v" * 1025,
        "tags": ["a"],
    }
    attributes.update({f"extra{i}": "x" for i in range(10)})
    emit_update("acme-corp", attributes=attributes)
    (body,) = consumer_update_bodies(consumer_update_exporter)
    assert body["attributes"] == {
        "plan": "pro",
        "seats": "5",
        "ratio": "0.5",
        "trial": "false",
        "region": None,
        "tier": None,
        **{f"extra{i}": "x" for i in range(4)},
    }


def test_set_consumer_merges_calls_for_same_identifier(consumer_update_exporter: InMemoryLogRecordExporter):
    init_consumer()
    set_consumer("other", name="Other", attributes={"plan": "free"})
    set_consumer("acme-corp", name="Acme", group="enterprise", attributes={"plan": "free", "seats": "5"})
    set_consumer("acme-corp", attributes={"plan": "pro"})
    emit_consumer_update_if_changed()
    assert consumer_update_bodies(consumer_update_exporter) == [
        {"identifier": "acme-corp", "name": "Acme", "group": "enterprise", "attributes": {"plan": "pro", "seats": "5"}}
    ]


def test_consumer_update_cache_evicts_least_recently_used(
    consumer_update_exporter: InMemoryLogRecordExporter, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(consumers, "MAX_CACHED_CONSUMERS", 2)
    for identifier in ["a", "b", "a", "c", "a", "b"]:
        emit_update(identifier, name=identifier)
    bodies = consumer_update_bodies(consumer_update_exporter)
    # "b" is evicted when "c" arrives because "a" was used more recently
    assert [body["identifier"] for body in bodies] == ["a", "b", "c", "b"]


def emit_update(identifier: str, **kwargs: Any) -> None:
    init_consumer()
    set_consumer(identifier, **kwargs)
    emit_consumer_update_if_changed()
