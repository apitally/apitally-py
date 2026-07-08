import logging
import logging.handlers
from collections.abc import Generator

import pytest
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.trace import SpanKind, Tracer

from apitally.shared.config import set_config
from apitally.shared.log_processor import (
    MAX_BUFFERED_LOGS,
    ApitallyLogRecordProcessor,
    install_root_handler,
    uninstall_root_handler,
)
from apitally.shared.span_processor import ApitallySpanProcessor
from tests.conftest import WRITE_TOKEN, unwrap


@pytest.fixture(autouse=True)
def cleanup() -> Generator[None, None, None]:
    yield
    uninstall_root_handler()


@pytest.fixture()
def span_processor() -> ApitallySpanProcessor:
    return ApitallySpanProcessor(SpanProcessor())


@pytest.fixture()
def tracer(span_processor: ApitallySpanProcessor) -> Tracer:
    provider = TracerProvider()
    provider.add_span_processor(span_processor)
    return provider.get_tracer("test")


@pytest.fixture()
def log_exporter() -> InMemoryLogRecordExporter:
    return InMemoryLogRecordExporter()


@pytest.fixture()
def logger_provider(span_processor: ApitallySpanProcessor, log_exporter: InMemoryLogRecordExporter) -> LoggerProvider:
    provider = LoggerProvider()
    provider.add_log_record_processor(
        ApitallyLogRecordProcessor(SimpleLogRecordProcessor(log_exporter), span_processor)
    )
    return provider


@pytest.fixture()
def root_handler(logger_provider: LoggerProvider) -> LoggingHandler | None:
    set_config(write_token=WRITE_TOKEN)
    return install_root_handler(logger_provider)


def test_log_in_nested_span_carries_server_span_id(
    tracer: Tracer, log_exporter: InMemoryLogRecordExporter, root_handler: LoggingHandler | None
):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as server:
        with tracer.start_as_current_span("child") as child:
            logging.getLogger("myapp").warning("inside child")
        # Buffered until the SERVER span's response-stage decision
        assert log_exporter.get_finished_logs() == ()
    (exported,) = log_exporter.get_finished_logs()
    record = exported.log_record
    assert record.trace_id == server.get_span_context().trace_id != 0
    assert record.span_id == child.get_span_context().span_id
    assert unwrap(record.attributes)["apitally.request.server_span_id"] == format(
        server.get_span_context().span_id, "016x"
    )


def test_response_stage_dropped_request_logs_discarded(log_exporter: InMemoryLogRecordExporter):
    set_config(write_token=WRITE_TOKEN, sample_on_response=lambda span: False)
    span_processor = ApitallySpanProcessor(SpanProcessor())
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(span_processor)
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(
        ApitallyLogRecordProcessor(SimpleLogRecordProcessor(log_exporter), span_processor)
    )
    install_root_handler(logger_provider)
    with tracer_provider.get_tracer("test").start_as_current_span("GET /items", kind=SpanKind.SERVER):
        logging.getLogger("myapp").warning("inside request")
    assert log_exporter.get_finished_logs() == ()


def test_log_buffer_cap_drops_excess(
    tracer: Tracer, log_exporter: InMemoryLogRecordExporter, root_handler: LoggingHandler | None
):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        for i in range(MAX_BUFFERED_LOGS + 1):
            logging.getLogger("myapp").warning("log %d", i)
    assert len(log_exporter.get_finished_logs()) == MAX_BUFFERED_LOGS


def test_log_without_active_request_dropped(
    log_exporter: InMemoryLogRecordExporter, root_handler: LoggingHandler | None
):
    logging.getLogger("myapp").warning("no request")
    assert log_exporter.get_finished_logs() == ()


def test_apitally_scope_passes_without_request_context(
    logger_provider: LoggerProvider, log_exporter: InMemoryLogRecordExporter
):
    startup_logger = logger_provider.get_logger("apitally")
    startup_logger.emit(LogRecord(body="startup", severity_number=SeverityNumber.INFO))
    (exported,) = log_exporter.get_finished_logs()
    assert exported.log_record.body == "startup"


def test_self_logs_reach_user_handlers_but_not_export(
    tracer: Tracer, log_exporter: InMemoryLogRecordExporter, root_handler: LoggingHandler | None
):
    user_handler = logging.handlers.BufferingHandler(capacity=10)
    logging.getLogger().addHandler(user_handler)
    try:
        with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
            logging.getLogger("apitally.client").warning("sdk noise")
            logging.getLogger("opentelemetry.sdk").warning("otel noise")
    finally:
        logging.getLogger().removeHandler(user_handler)
    assert [r.getMessage() for r in user_handler.buffer] == ["sdk noise", "otel noise"]
    assert log_exporter.get_finished_logs() == ()


def test_capture_logs_false_installs_no_handler(logger_provider: LoggerProvider):
    set_config(write_token=WRITE_TOKEN, capture_logs=False)
    handlers_before = list(logging.getLogger().handlers)
    assert install_root_handler(logger_provider) is None
    assert logging.getLogger().handlers == handlers_before


def test_excluded_request_contributes_no_logs(
    tracer: Tracer, log_exporter: InMemoryLogRecordExporter, root_handler: LoggingHandler | None
):
    attributes = {"http.request.method": "GET", "url.path": "/healthz"}
    with tracer.start_as_current_span("GET /healthz", kind=SpanKind.SERVER, attributes=attributes):
        logging.getLogger("myapp").warning("inside excluded request")
    assert log_exporter.get_finished_logs() == ()
