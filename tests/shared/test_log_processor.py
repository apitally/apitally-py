import logging
import logging.handlers
from collections.abc import Generator
from unittest import mock

import pytest
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
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
def root_handler(logger_provider: LoggerProvider, span_processor: ApitallySpanProcessor) -> LoggingHandler | None:
    set_config(write_token=WRITE_TOKEN)
    return install_root_handler(logger_provider, span_processor)


def test_logs_in_nested_spans_include_server_span_id(
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


def test_logs_discarded_when_sample_on_response_returns_false(log_exporter: InMemoryLogRecordExporter):
    set_config(write_token=WRITE_TOKEN, sample_on_response=lambda span: False)
    span_processor = ApitallySpanProcessor(SpanProcessor())
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(span_processor)
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(
        ApitallyLogRecordProcessor(SimpleLogRecordProcessor(log_exporter), span_processor)
    )
    install_root_handler(logger_provider, span_processor)
    with tracer_provider.get_tracer("test").start_as_current_span("GET /items", kind=SpanKind.SERVER):
        logging.getLogger("myapp").warning("inside request")
    assert log_exporter.get_finished_logs() == ()


def test_buffer_cap_drops_excess_logs(
    tracer: Tracer, log_exporter: InMemoryLogRecordExporter, root_handler: LoggingHandler | None
):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        for i in range(MAX_BUFFERED_LOGS + 1):
            logging.getLogger("myapp").warning("log %d", i)
    assert len(log_exporter.get_finished_logs()) == MAX_BUFFERED_LOGS


def test_logs_without_active_request_dropped(
    log_exporter: InMemoryLogRecordExporter, root_handler: LoggingHandler | None
):
    logging.getLogger("myapp").warning("no request")
    assert log_exporter.get_finished_logs() == ()


def test_apitally_scope_logs_exported_without_request_context(
    logger_provider: LoggerProvider, log_exporter: InMemoryLogRecordExporter
):
    startup_logger = logger_provider.get_logger("apitally")
    startup_logger.emit(LogRecord(body="startup", severity_number=SeverityNumber.INFO))
    (exported,) = log_exporter.get_finished_logs()
    assert exported.log_record.body == "startup"


def test_sdk_and_otel_logs_not_exported(
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


def test_capture_logs_false_installs_no_handler(logger_provider: LoggerProvider, span_processor: ApitallySpanProcessor):
    set_config(write_token=WRITE_TOKEN, capture_logs=False)
    handlers_before = list(logging.getLogger().handlers)
    assert install_root_handler(logger_provider, span_processor) is None
    assert logging.getLogger().handlers == handlers_before


def test_shutdown_flushes_queued_logs(log_exporter: InMemoryLogRecordExporter):
    set_config(write_token=WRITE_TOKEN)
    span_processor = ApitallySpanProcessor(SpanProcessor())
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(span_processor)
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(
        ApitallyLogRecordProcessor(BatchLogRecordProcessor(log_exporter), span_processor)
    )
    install_root_handler(logger_provider, span_processor)
    with tracer_provider.get_tracer("test").start_as_current_span("GET /items", kind=SpanKind.SERVER):
        logging.getLogger("myapp").warning("during request")
    assert log_exporter.get_finished_logs() == ()  # released to the batch processor, still queued
    logger_provider.shutdown()
    (exported,) = log_exporter.get_finished_logs()
    assert exported.log_record.body == "during request"


def test_no_logs_exported_for_excluded_requests(
    tracer: Tracer, log_exporter: InMemoryLogRecordExporter, root_handler: LoggingHandler | None
):
    assert root_handler is not None
    attributes = {"http.request.method": "GET", "url.path": "/healthz"}
    with mock.patch.object(root_handler, "emit", wraps=root_handler.emit) as emit_spy:
        with tracer.start_as_current_span("GET /healthz", kind=SpanKind.SERVER, attributes=attributes):
            logging.getLogger("myapp").warning("inside excluded request")
    # The kept-request filter drops the record before the handler translates it
    emit_spy.assert_not_called()
    assert log_exporter.get_finished_logs() == ()


def test_loguru_logs_captured_with_extra(
    tracer: Tracer, log_exporter: InMemoryLogRecordExporter, root_handler: LoggingHandler | None
):
    from loguru import logger

    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        logger.bind(user_id=42).info("Hello {name}", name="world")
    (exported,) = log_exporter.get_finished_logs()
    record = exported.log_record
    assert record.body == "Hello world"
    assert unwrap(record.attributes)["user_id"] == 42


def test_loguru_logs_include_exception_attributes(
    tracer: Tracer, log_exporter: InMemoryLogRecordExporter, root_handler: LoggingHandler | None
):
    from loguru import logger

    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER):
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("Something failed")
    (exported,) = log_exporter.get_finished_logs()
    attributes = unwrap(exported.log_record.attributes)
    assert attributes["exception.type"] == "ValueError"
    stacktrace = attributes["exception.stacktrace"]
    assert isinstance(stacktrace, str)
    assert "boom" in stacktrace
