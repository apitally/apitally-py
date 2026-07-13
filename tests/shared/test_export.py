import gzip
import logging
import socket
import threading
import time
from collections import deque
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.instrumentation.utils import is_instrumentation_enabled
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
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

from apitally.shared import activation, export, metrics, startup
from apitally.shared.config import set_config
from apitally.shared.context import get_server_span_processor
from apitally.shared.export import (
    ENCODE_CHUNK_SIZE,
    EXPORT_INTERVAL_HEADER,
    MAX_EXPORT_INTERVAL,
    MAX_LOG_VALUE_LENGTH,
    MAX_SENDS_PER_CYCLE,
    MIN_EXPORT_INTERVAL,
    ExportWorker,
    SpoolLogExporter,
    SpoolSpanExporter,
)
from apitally.shared.exporter import ApitallySpanExporter
from apitally.shared.redaction import REDACTED
from apitally.shared.span_processor import ApitallySpanProcessor
from apitally.shared.spool import MAX_RETRY_TIME_AFTER_FIRST_ATTEMPT, MAX_UNCOMPRESSED_FILE_SIZE, Spool
from tests.conftest import CONTRIB_SCOPE, WRITE_TOKEN, StubOTLPServer, installed, read_spool_payload, unwrap


@pytest.fixture
def spool() -> Iterator[Spool]:
    spool = Spool()
    yield spool
    spool.clear()


def make_worker(spool: Spool, endpoint: str) -> ExportWorker:
    set_config(write_token=WRITE_TOKEN, env="dev", otlp_endpoint=endpoint)
    processor_stub = SimpleNamespace(downstream=SimpleNamespace(force_flush=lambda timeout_millis=30_000: True))
    return ExportWorker(spool, cast("Any", processor_stub), cast("Any", processor_stub), env="dev")


def read_trace_request(spool: Spool) -> ExportTraceServiceRequest:
    spool.rotate_for_export()
    (file,) = [file for file in spool.pending_files() if file.signal == "traces"]
    return ExportTraceServiceRequest.FromString(read_spool_payload(file))


def read_log_request(spool: Spool) -> ExportLogsServiceRequest:
    spool.rotate_for_export()
    (file,) = [file for file in spool.pending_files() if file.signal == "logs"]
    return ExportLogsServiceRequest.FromString(read_spool_payload(file))


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


def test_span_batch_is_written_to_spool_as_parseable_payload(spool: Spool) -> None:
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
    payload = read_spool_payload(file)
    assert b"secret123" not in payload
    assert b"hunter2" not in payload
    assert REDACTED.encode() in payload
    assert b"application/json" in payload


def test_log_batch_is_written_to_spool_as_parseable_payload(spool: Spool) -> None:
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


def test_oversized_log_body_is_truncated(spool: Spool) -> None:
    emit_log(spool, "x" * (MAX_LOG_VALUE_LENGTH + 1000))
    (record,) = read_log_records(spool)
    assert record.body.string_value == "x" * MAX_LOG_VALUE_LENGTH
    assert record.severity_text == "INFO"
    assert any(kv.key == "code.file.path" for kv in record.attributes)


def test_oversized_log_attribute_value_is_truncated(spool: Spool) -> None:
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


def test_export_cycle_posts_all_three_signals_in_lockstep(spool: Spool, otlp_server: StubOTLPServer) -> None:
    worker = make_worker(spool, otlp_server.url)
    metrics.setup(Resource.create({}))
    metrics.attach_reader(spool)
    metrics.record_request("GET", "/a", 200, consumer=None, duration=0.1)
    spool.append("traces", b"trace-payload")
    spool.append("logs", b"log-payload")
    worker.run_cycle(None)
    assert sorted(otlp_server.paths()) == ["/v1/logs", "/v1/metrics", "/v1/traces"]
    _, headers, body = next(request for request in otlp_server.requests if request[0] == "/v1/traces")
    assert headers["Authorization"] == f"Bearer {WRITE_TOKEN}"
    assert headers["Apitally-Env"] == "dev"
    assert headers["Content-Type"] == "application/x-protobuf"
    assert headers["Content-Encoding"] == "gzip"
    assert headers["User-Agent"].startswith("apitally-py/")
    assert gzip.decompress(body) == b"trace-payload"
    assert spool.pending_files() == []


def test_failed_send_retries_next_cycle_with_identical_bytes(spool: Spool, otlp_server: StubOTLPServer) -> None:
    failures = deque([503])
    otlp_server.respond = lambda path: (failures.popleft() if failures else 200, {})
    worker = make_worker(spool, otlp_server.url)
    spool.append("traces", b"trace-payload")
    worker.run_cycle(None)
    assert len(spool.pending_files()) == 1
    worker.run_cycle(None)
    assert spool.pending_files() == []
    first_body, second_body = [body for _, _, body in otlp_server.requests]
    assert first_body == second_body
    assert gzip.decompress(first_body) == b"trace-payload"


def test_connection_error_keeps_files_and_ends_cycle(spool: Spool) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    unused_port = sock.getsockname()[1]
    sock.close()
    worker = make_worker(spool, f"http://127.0.0.1:{unused_port}")
    spool.append("traces", b"trace-payload")
    spool.rotate_for_export()
    spool.append("logs", b"log-payload")
    worker.run_cycle(None)
    files = {file.signal: file for file in spool.pending_files()}
    assert set(files) == {"traces", "logs"}
    assert files["traces"].first_attempt_at is not None
    assert files["logs"].first_attempt_at is None


def test_outage_sends_one_probe_per_cycle_without_accumulating_files(spool: Spool, otlp_server: StubOTLPServer) -> None:
    otlp_server.respond = lambda path: (503, {})
    worker = make_worker(spool, otlp_server.url)
    for _ in range(3):
        spool.append("traces", b"recorded-during-outage")
        worker.run_cycle(None)
    assert len(spool.pending_files()) == 1
    assert otlp_server.paths() == ["/v1/traces"] * 3


def test_sends_per_cycle_are_capped(spool: Spool, otlp_server: StubOTLPServer) -> None:
    worker = make_worker(spool, otlp_server.url)
    for _ in range(MAX_SENDS_PER_CYCLE + 2):
        spool.append("traces", b"x" * MAX_UNCOMPRESSED_FILE_SIZE)
    worker.send_pending(None)
    assert len(otlp_server.paths()) == MAX_SENDS_PER_CYCLE
    assert len(spool.pending_files()) == 1


def test_stop_during_pacing_wait_ends_drain(
    spool: Spool, otlp_server: StubOTLPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = make_worker(spool, otlp_server.url)
    spool.append("traces", b"trace-payload")
    spool.append("logs", b"log-payload")
    spool.rotate_for_export()
    stop_event = threading.Event()
    monkeypatch.setattr(stop_event, "wait", lambda timeout=None: True)
    worker.send_pending(stop_event)
    assert len(otlp_server.paths()) == 1
    assert len(spool.pending_files()) == 1


def test_expired_file_dropped_before_sending_while_never_attempted_file_delivers(
    spool: Spool, otlp_server: StubOTLPServer, caplog: pytest.LogCaptureFixture
) -> None:
    worker = make_worker(spool, otlp_server.url)
    spool.append("traces", b"attempted-once")
    spool.append("logs", b"never-attempted")
    spool.rotate_for_export()
    files = {file.signal: file for file in spool.pending_files()}
    files["traces"].first_attempt_at = time.monotonic() - MAX_RETRY_TIME_AFTER_FIRST_ATTEMPT - 1
    with caplog.at_level(logging.WARNING):
        worker.send_pending(None)
    assert otlp_server.paths() == ["/v1/logs"]
    assert spool.pending_files() == []
    assert any("could not be delivered" in record.message for record in caplog.records)


def test_permanent_failure_drops_only_that_file_and_warns_once(
    spool: Spool, otlp_server: StubOTLPServer, caplog: pytest.LogCaptureFixture
) -> None:
    otlp_server.respond = lambda path: (402, {}) if path == "/v1/traces" else (200, {})
    worker = make_worker(spool, otlp_server.url)
    spool.append("traces", b"trace-payload")
    spool.append("logs", b"log-payload")
    with caplog.at_level(logging.WARNING):
        worker.run_cycle(None)
        assert spool.pending_files() == []
        assert otlp_server.paths() == ["/v1/traces", "/v1/logs"]
        spool.append("traces", b"more-trace-payload")
        worker.run_cycle(None)
    assert spool.pending_files() == []
    assert len([record for record in caplog.records if "rejected" in record.message]) == 1


def test_interval_header_applies_with_clamping(spool: Spool, otlp_server: StubOTLPServer) -> None:
    header_value = {"value": "5"}
    otlp_server.respond = lambda path: (200, {EXPORT_INTERVAL_HEADER: header_value["value"]})
    worker = make_worker(spool, otlp_server.url)
    for value, expected_interval in (
        ("5", 5.0),
        ("10000", float(MAX_EXPORT_INTERVAL)),
        ("1", float(MIN_EXPORT_INTERVAL)),
        ("not-a-number", float(MIN_EXPORT_INTERVAL)),
    ):
        header_value["value"] = value
        spool.append("traces", b"trace-payload")
        worker.run_cycle(None)
        assert worker.interval == expected_interval


def test_first_export_fires_shortly_after_start(
    spool: Spool, otlp_server: StubOTLPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export, "INITIAL_EXPORT_DELAY", 0.05)
    worker = make_worker(spool, otlp_server.url)
    spool.append("traces", b"early-payload")
    worker.start()
    try:
        assert unwrap(worker.thread).name == "ApitallyExportWorker"
        deadline = time.time() + 5
        while not otlp_server.requests and time.time() < deadline:
            time.sleep(0.01)
    finally:
        worker.stop()
    assert otlp_server.paths() == ["/v1/traces"]


def test_export_cycle_suppresses_instrumentation(spool: Spool, otlp_server: StubOTLPServer) -> None:
    set_config(write_token=WRITE_TOKEN, env="dev", otlp_endpoint=otlp_server.url)
    suppressed_flags: list[bool] = []
    processor_stub = SimpleNamespace(
        downstream=SimpleNamespace(
            force_flush=lambda timeout_millis=30_000: suppressed_flags.append(not is_instrumentation_enabled())
        )
    )
    worker = ExportWorker(spool, cast("Any", processor_stub), cast("Any", processor_stub), env="dev")
    worker.session.hooks["response"] = [
        lambda response, **kwargs: suppressed_flags.append(not is_instrumentation_enabled())
    ]
    spool.append("traces", b"trace-payload")
    worker.run_cycle(None)
    assert suppressed_flags == [True, True, True]
    assert is_instrumentation_enabled()


def test_shutdown_performs_final_drain_and_stops_thread(
    spool: Spool, otlp_server: StubOTLPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export, "INITIAL_EXPORT_DELAY", 60.0)
    worker = make_worker(spool, otlp_server.url)
    worker.start()
    spool.append("traces", b"final-payload")
    worker.shutdown()
    assert unwrap(worker.thread).is_alive() is False
    assert otlp_server.paths() == ["/v1/traces"]
    assert spool.pending_files() == []


def test_unreadable_file_is_dropped_without_blocking_the_queue(
    spool: Spool, otlp_server: StubOTLPServer, caplog: pytest.LogCaptureFixture
) -> None:
    worker = make_worker(spool, otlp_server.url)
    spool.append("traces", b"trace-payload")
    spool.rotate_for_export()
    spool.append("logs", b"log-payload")
    files = {file.signal: file for file in spool.pending_files()}
    files["traces"].sink.close()
    with caplog.at_level(logging.WARNING, logger="apitally.shared.export"):
        worker.run_cycle(None)
    assert otlp_server.paths() == ["/v1/logs"]
    assert spool.pending_files() == []
    assert any("traces" in record.getMessage() for record in caplog.records)


def test_start_after_timed_out_stop_replaces_stuck_thread(
    spool: Spool, otlp_server: StubOTLPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def blocked_ok(path: str) -> tuple[int, dict[str, str]]:
        release.wait(10)
        return (200, {})

    otlp_server.respond = blocked_ok
    monkeypatch.setattr(export, "INITIAL_EXPORT_DELAY", 0.01)
    worker = make_worker(spool, otlp_server.url)
    spool.append("traces", b"trace-payload")
    worker.start()
    deadline = time.time() + 5
    while not otlp_server.requests and time.time() < deadline:
        time.sleep(0.005)
    worker.stop(timeout=0.05)
    stuck_thread = unwrap(worker.thread)
    assert stuck_thread.is_alive()
    worker.start()
    new_thread = unwrap(worker.thread)
    assert new_thread is not stuck_thread
    assert new_thread.is_alive()
    release.set()
    worker.stop()
    stuck_thread.join(5)
    assert not stuck_thread.is_alive()


starlette_required = pytest.mark.skipif(
    not installed("starlette", "opentelemetry.instrumentation.starlette"),
    reason="end-to-end tests use the Starlette adapter",
)


def create_starlette_client(otlp_server: StubOTLPServer, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Any:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from apitally.starlette import init_apitally

    async def get_item(request: Request) -> JSONResponse:
        logging.getLogger("myapp").warning("handling item")
        return JSONResponse({"item_id": request.path_params["item_id"]})

    async def login(request: Request) -> JSONResponse:
        await request.json()
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/items/{item_id}", get_item), Route("/login", login, methods=["POST"])])
    monkeypatch.setenv("APITALLY_OTLP_ENDPOINT", otlp_server.url)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(export, "INITIAL_EXPORT_DELAY", 60.0)
    init_apitally(app, write_token=WRITE_TOKEN, **kwargs)
    return TestClient(app)


def decoded_records(otlp_server: StubOTLPServer, path: str, message_type: Any) -> list[Any]:
    return [
        message_type.FromString(gzip.decompress(body))
        for request_path, _, body in otlp_server.requests
        if request_path == path
    ]


@starlette_required
def test_end_to_end_request_delivers_all_three_signals_decoded(
    otlp_server: StubOTLPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    with create_starlette_client(otlp_server, monkeypatch) as client:
        assert client.get("/items/42").status_code == 200
        assert unwrap(activation.spool).in_memory is False
        assert otlp_server.requests == []

    # Client exit runs lifespan shutdown, which flushes and sends all buffered telemetry
    assert {"/v1/traces", "/v1/logs", "/v1/metrics"} <= set(otlp_server.paths())

    (trace_request,) = decoded_records(otlp_server, "/v1/traces", ExportTraceServiceRequest)
    (server_span,) = [
        span
        for rs in trace_request.resource_spans
        for ss in rs.scope_spans
        for span in ss.spans
        if not span.parent_span_id
    ]
    attributes = {kv.key: kv.value for kv in server_span.attributes}
    assert attributes["http.route"].string_value == "/items/{item_id}"
    assert attributes["http.response.status_code"].int_value == 200

    (log_request,) = decoded_records(otlp_server, "/v1/logs", ExportLogsServiceRequest)
    records = [record for rl in log_request.resource_logs for sl in rl.scope_logs for record in sl.log_records]
    assert any(record.event_name == startup.EVENT_NAME for record in records)
    assert any(record.body.string_value == "handling item" for record in records)

    (metrics_request,) = decoded_records(otlp_server, "/v1/metrics", ExportMetricsServiceRequest)
    metric_names = {
        metric.name for rm in metrics_request.resource_metrics for sm in rm.scope_metrics for metric in sm.metrics
    }
    assert "http.server.request.duration" in metric_names


@starlette_required
def test_end_to_end_sensitive_body_redacted_in_spool_files_and_sent_payloads(
    otlp_server: StubOTLPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    with create_starlette_client(otlp_server, monkeypatch, log_request_body=True) as client:
        assert client.post("/login", json={"password": "hunter2"}).status_code == 200
        spool = unwrap(activation.spool)
        unwrap(activation.span_processor).downstream.force_flush()
        spool.close_current_files()
        spool_payloads = b"".join(read_spool_payload(file) for file in spool.pending_files() if file.signal == "traces")
        assert b"hunter2" not in spool_payloads
        assert b"apitally.request.body" in spool_payloads
        assert REDACTED.encode() in spool_payloads

    sent_payloads = b"".join(gzip.decompress(body) for _, _, body in otlp_server.requests)
    assert sent_payloads
    assert b"hunter2" not in sent_payloads
    assert b"apitally.request.body" in sent_payloads
    assert REDACTED.encode() in sent_payloads


@starlette_required
def test_end_to_end_downtime_data_delivered_byte_identical_after_recovery(
    otlp_server: StubOTLPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    otlp_server.respond = lambda path: (503, {})
    with create_starlette_client(otlp_server, monkeypatch) as client:
        assert client.get("/items/42").status_code == 200
        worker = unwrap(activation.export_worker)
        worker.run_cycle(None)
        assert unwrap(activation.spool).pending_files()
        first_attempt_bodies = {path: body for path, _, body in otlp_server.requests}

        otlp_server.respond = lambda path: (200, {})
        worker.run_cycle(None)
        worker.run_cycle(None)
        assert unwrap(activation.spool).pending_files() == []

    for path, body in first_attempt_bodies.items():
        delivered = [b for p, _, b in otlp_server.requests[len(first_attempt_bodies) :] if p == path]
        assert body in delivered

    trace_request = decoded_records(otlp_server, "/v1/traces", ExportTraceServiceRequest)[-1]
    span_names = [span.name for rs in trace_request.resource_spans for ss in rs.scope_spans for span in ss.spans]
    assert any("/items" in name for name in span_names)
