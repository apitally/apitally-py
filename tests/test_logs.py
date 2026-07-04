import logging
import logging.handlers

import pytest
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.trace import SpanKind

from apitally.shared.config import configure
from apitally.shared.log_processor import (
    ApitallyLogRecordProcessor,
    install_root_handler,
    uninstall_root_handler,
)
from apitally.shared.span_processor import ApitallySpanProcessor, server_span_var


TOKEN = "apt_" + "a" * 24


@pytest.fixture(autouse=True)
def cleanup():
    yield
    uninstall_root_handler()
    server_span_var.set(None)


@pytest.fixture()
def span_processor():
    return ApitallySpanProcessor(SpanProcessor())


@pytest.fixture()
def tracer(span_processor):
    provider = TracerProvider()
    provider.add_span_processor(span_processor)
    return provider.get_tracer("test")


@pytest.fixture()
def log_exporter():
    return InMemoryLogRecordExporter()


@pytest.fixture()
def logger_provider(span_processor, log_exporter):
    provider = LoggerProvider()
    provider.add_log_record_processor(
        ApitallyLogRecordProcessor(SimpleLogRecordProcessor(log_exporter), span_processor)
    )
    return provider


@pytest.fixture()
def root_handler(logger_provider):
    configure(write_token=TOKEN)
    return install_root_handler(logger_provider)


def test_log_in_nested_span_carries_server_span_id(tracer, log_exporter, root_handler):
    with tracer.start_as_current_span("GET /items", kind=SpanKind.SERVER) as server:
        with tracer.start_as_current_span("child") as child:
            logging.getLogger("myapp").warning("inside child")
    (exported,) = log_exporter.get_finished_logs()
    record = exported.log_record
    assert record.trace_id == server.context.trace_id != 0
    assert record.span_id == child.context.span_id
    assert record.attributes["apitally.request.server_span_id"] == format(server.context.span_id, "016x")


def test_log_without_active_request_dropped(log_exporter, root_handler):
    logging.getLogger("myapp").warning("no request")
    assert log_exporter.get_finished_logs() == ()


def test_apitally_scope_passes_without_request_context(logger_provider, log_exporter):
    startup_logger = logger_provider.get_logger("apitally")
    startup_logger.emit(LogRecord(body="startup", severity_number=SeverityNumber.INFO))
    (exported,) = log_exporter.get_finished_logs()
    assert exported.log_record.body == "startup"


def test_self_logs_reach_user_handlers_but_not_export(tracer, log_exporter, root_handler):
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


def test_capture_logs_false_installs_no_handler(logger_provider):
    configure(write_token=TOKEN, capture_logs=False)
    handlers_before = list(logging.getLogger().handlers)
    assert install_root_handler(logger_provider) is None
    assert logging.getLogger().handlers == handlers_before


def test_excluded_request_contributes_no_logs(tracer, log_exporter, root_handler):
    attributes = {"http.request.method": "GET", "url.path": "/healthz"}
    with tracer.start_as_current_span("GET /healthz", kind=SpanKind.SERVER, attributes=attributes):
        logging.getLogger("myapp").warning("inside excluded request")
    assert log_exporter.get_finished_logs() == ()
