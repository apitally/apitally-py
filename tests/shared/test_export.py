import gzip
import logging
from collections.abc import Iterator

import pytest
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord as PB2LogRecord
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import SpanKind

from apitally.shared.config import set_config
from apitally.shared.export import ENCODE_CHUNK_SIZE, MAX_LOG_VALUE_LENGTH, SpoolLogExporter, SpoolSpanExporter
from apitally.shared.exporter import ApitallySpanExporter
from apitally.shared.redaction import REDACTED
from apitally.shared.span_processor import ApitallySpanProcessor, get_server_span_processor
from apitally.shared.spool import Spool
from tests.conftest import CONTRIB_SCOPE, WRITE_TOKEN, unwrap


@pytest.fixture
def spool() -> Iterator[Spool]:
    spool = Spool()
    yield spool
    spool.clear()


def read_trace_request(spool: Spool) -> ExportTraceServiceRequest:
    spool.rotate_for_export()
    (file,) = [file for file in spool.pending_files() if file.signal == "traces"]
    return ExportTraceServiceRequest.FromString(gzip.decompress(file.read_bytes()))


def read_log_request(spool: Spool) -> ExportLogsServiceRequest:
    spool.rotate_for_export()
    (file,) = [file for file in spool.pending_files() if file.signal == "logs"]
    return ExportLogsServiceRequest.FromString(gzip.decompress(file.read_bytes()))


def read_log_records(spool: Spool) -> list[PB2LogRecord]:
    request = read_log_request(spool)
    return [record for rl in request.resource_logs for sl in rl.scope_logs for record in sl.log_records]


def make_log_provider(spool: Spool) -> LoggerProvider:
    provider = LoggerProvider(resource=Resource.create({}))
    provider.add_log_record_processor(SimpleLogRecordProcessor(SpoolLogExporter(spool)))
    return provider


def emit_log(spool: Spool, msg: str) -> None:
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=make_log_provider(spool), log_code_attributes=True)
    handler.emit(
        logging.LogRecord(
            name="app", level=logging.INFO, pathname=__file__, lineno=1, msg=msg, args=None, exc_info=None
        )
    )


def test_span_batch_lands_in_spool_as_parseable_payload(spool: Spool) -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(ApitallySpanExporter(SpoolSpanExporter(spool))))
    with provider.get_tracer(CONTRIB_SCOPE).start_as_current_span("GET /items", kind=SpanKind.SERVER):
        pass
    request = read_trace_request(spool)
    (resource_spans,) = request.resource_spans
    assert [span.name for ss in resource_spans.scope_spans for span in ss.spans] == ["GET /items"]
    assert "service.name" in {kv.key for kv in resource_spans.resource.attributes}


def test_stashed_body_and_headers_reach_spool_redacted(spool: Spool) -> None:
    set_config(write_token=WRITE_TOKEN, log_request_headers=True, log_request_body=True)
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(
        ApitallySpanProcessor(SimpleSpanProcessor(ApitallySpanExporter(SpoolSpanExporter(spool))))
    )
    tracer = provider.get_tracer(CONTRIB_SCOPE)
    with tracer.start_as_current_span("POST /login", kind=SpanKind.SERVER) as span:
        processor = unwrap(get_server_span_processor())
        processor.update_stash(
            span.get_span_context().span_id,
            request_headers={"authorization": ["Bearer secret123"], "accept": ["application/json"]},
            request_body=b'{"password": "hunter2"}',
        )
    spool.rotate_for_export()
    (file,) = spool.pending_files()
    payload = gzip.decompress(file.read_bytes())
    assert b"secret123" not in payload
    assert b"hunter2" not in payload
    assert REDACTED.encode() in payload
    assert b"application/json" in payload


def test_log_batch_lands_in_spool_as_parseable_payload(spool: Spool) -> None:
    emit_log(spool, "something happened")
    (record,) = read_log_records(spool)
    assert record.body.string_value == "something happened"


def test_large_batch_appends_multiple_payloads_without_loss(spool: Spool) -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    memory_exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(memory_exporter))
    tracer = provider.get_tracer(CONTRIB_SCOPE)
    span_names = [f"span-{i}" for i in range(ENCODE_CHUNK_SIZE + 8)]
    for name in span_names:
        with tracer.start_as_current_span(name):
            pass
    SpoolSpanExporter(spool).export(memory_exporter.get_finished_spans())
    request = read_trace_request(spool)
    assert len(request.resource_spans) == 2
    exported_names = [span.name for rs in request.resource_spans for ss in rs.scope_spans for span in ss.spans]
    assert sorted(exported_names) == sorted(span_names)


def test_oversized_log_body_is_truncated_at_encode_time(spool: Spool) -> None:
    emit_log(spool, "x" * (MAX_LOG_VALUE_LENGTH + 1000))
    (record,) = read_log_records(spool)
    assert record.body.string_value == "x" * MAX_LOG_VALUE_LENGTH
    assert record.severity_text == "INFO"
    assert any(kv.key == "code.file.path" for kv in record.attributes)


def test_oversized_log_attribute_value_is_truncated_at_encode_time(spool: Spool) -> None:
    provider = make_log_provider(spool)
    provider.get_logger("app").emit(body="hello", attributes={"detail": "y" * (MAX_LOG_VALUE_LENGTH + 1000)})
    (record,) = read_log_records(spool)
    attributes = {kv.key: kv.value.string_value for kv in record.attributes}
    assert attributes["detail"] == "y" * MAX_LOG_VALUE_LENGTH
    assert record.body.string_value == "hello"


def test_apitally_scope_records_are_exempt_from_truncation(spool: Spool) -> None:
    provider = make_log_provider(spool)
    long_body = "{" + "a" * (MAX_LOG_VALUE_LENGTH * 2) + "}"
    provider.get_logger("apitally").emit(body=long_body, event_name="apitally.startup")
    (record,) = read_log_records(spool)
    assert record.body.string_value == long_body
