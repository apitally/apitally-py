import contextlib
import logging
import sys
from collections.abc import Callable, MutableMapping
from types import CodeType, FrameType
from typing import TYPE_CHECKING, cast

from opentelemetry import trace
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor, ReadableLogRecord, ReadWriteLogRecord
from opentelemetry.util.types import AnyValue

from apitally.shared.config import get_config
from apitally.shared.context import is_server_span_kept
from apitally.shared.span_processor import ApitallySpanProcessor


if TYPE_CHECKING:
    from loguru import Message as LoguruMessage


logger = logging.getLogger(__name__)

SERVER_SPAN_ID_ATTRIBUTE = "apitally.request.server_span_id"
SDK_LOGGER_NAMESPACES = ("apitally", "opentelemetry")
MAX_BUFFERED_LOGS = 1_000
MAX_LOG_VALUE_LENGTH = 2048

installed_handler: LoggingHandler | None = None
loguru_sink_id: int | None = None
loguru_log_code: CodeType | None = None


class ApitallyLoggingHandler(LoggingHandler):
    def handle(self, record: logging.LogRecord) -> bool:
        # Records propagated from a loguru sink into stdlib logging are already captured by the loguru sink
        if loguru_log_code is not None and find_frame(loguru_log_code) is not None:
            return False
        # A root handler hides the stdlib lastResort fallback, so restore it for loggers without other handlers
        last_resort = logging.lastResort
        if last_resort is not None and record.levelno >= last_resort.level and not self.has_other_handlers(record):
            last_resort.handle(record)
        return super().handle(record)

    def has_other_handlers(self, record: logging.LogRecord) -> bool:
        logger: logging.Logger | None = logging.getLogger(record.name)
        while logger is not None:
            if any(handler is not self for handler in logger.handlers):
                return True
            logger = logger.parent
        return False


def install_root_handler(
    logger_provider: LoggerProvider, span_processor: ApitallySpanProcessor
) -> LoggingHandler | None:
    """Bridge stdlib logging into the private LoggerProvider."""
    global installed_handler
    config = get_config()
    if not config.capture_logs:
        return None
    if installed_handler is None:
        handler = ApitallyLoggingHandler(
            level=logging.NOTSET, logger_provider=logger_provider, log_code_attributes=True
        )
        handler.addFilter(is_application_log)
        handler.addFilter(make_kept_request_filter(span_processor))
        logging.getLogger().addHandler(handler)
        installed_handler = handler
        install_loguru_sink(handler)
    return installed_handler


def uninstall_root_handler() -> None:
    global installed_handler
    uninstall_loguru_sink()
    if installed_handler is not None:
        logging.getLogger().removeHandler(installed_handler)
        installed_handler = None


def install_loguru_sink(handler: LoggingHandler) -> None:
    global loguru_sink_id, loguru_log_code
    try:
        from loguru import logger as loguru_logger
    except ImportError:
        return

    def sink(message: "LoguruMessage") -> None:
        # Stdlib records forwarded by an intercept handler are already captured by the root handler
        frame = find_frame(logging.Logger.callHandlers.__code__)
        if frame is not None and reaches_handler(frame.f_locals["self"], handler):
            return
        record = message.record
        exception = record["exception"]
        exc_info = (
            (exception.type, exception.value, exception.traceback)
            if exception is not None and exception.type is not None and exception.value is not None
            else None
        )
        log_record = logging.LogRecord(
            name=record["name"] or "loguru",
            level=record["level"].no,
            pathname=record["file"].path,
            lineno=record["line"],
            msg=record["message"],
            args=(),
            exc_info=exc_info,
            func=record["function"],
        )
        log_record.created = record["time"].timestamp()
        for key, value in record["extra"].items():
            if key not in log_record.__dict__:
                log_record.__dict__[key] = value
        # Loguru writes to its own sinks, so the lastResort fallback must not apply here
        LoggingHandler.handle(handler, log_record)

    loguru_sink_id = loguru_logger.add(sink, level=0)
    loguru_log_code = loguru_logger._log.__code__  # ty: ignore[unresolved-attribute]


def uninstall_loguru_sink() -> None:
    global loguru_sink_id, loguru_log_code
    if loguru_sink_id is not None:
        from loguru import logger as loguru_logger

        with contextlib.suppress(ValueError):
            loguru_logger.remove(loguru_sink_id)
        loguru_sink_id = None
        loguru_log_code = None


def find_frame(code: CodeType) -> FrameType | None:
    frame: FrameType | None = sys._getframe(1)
    while frame is not None and frame.f_code is not code:
        frame = frame.f_back
    return frame


def reaches_handler(logger: logging.Logger, handler: logging.Handler) -> bool:
    while logger.propagate and logger.parent is not None:
        logger = logger.parent
    return handler in logger.handlers


def is_application_log(record: logging.LogRecord) -> bool:
    # SDK and OTel own logs stay out of the export; they still reach the user's own handlers
    return record.name.partition(".")[0] not in SDK_LOGGER_NAMESPACES


def make_kept_request_filter(span_processor: ApitallySpanProcessor) -> Callable[[logging.LogRecord], bool]:
    """Drop records that on_emit would discard anyway, before the handler translates them."""

    def is_in_kept_request(record: logging.LogRecord) -> bool:
        if is_server_span_kept():
            return True
        span_id = trace.get_current_span().get_span_context().span_id
        return bool(span_id) and span_processor.resolve_server_span_id(span_id) is not None

    return is_in_kept_request


class ApitallyLogRecordProcessor(LogRecordProcessor):
    """Stamps the SERVER span id on request-scoped records and drops the rest."""

    def __init__(self, downstream: LogRecordProcessor, span_processor: ApitallySpanProcessor) -> None:
        # Settable so fork re-activation can swap in a fresh batch processor
        self.downstream = downstream
        self.span_processor = span_processor
        self.pending: dict[int, list[ReadWriteLogRecord]] = {}
        span_processor.on_request_finished = self.finish_request

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        try:
            record = log_record.log_record
            server_span_id = self.span_processor.resolve_server_span_id(record.span_id) if record.span_id else None
            if server_span_id is None:
                # Scope "apitally" passes without request context to preserve the startup event
                if log_record.instrumentation_scope is None or log_record.instrumentation_scope.name != "apitally":
                    return
            elif record.attributes is not None:
                # ReadWriteLogRecord.__post_init__ replaces attributes with mutable BoundedAttributes
                attributes = cast(MutableMapping[str, AnyValue], record.attributes)
                attributes[SERVER_SPAN_ID_ATTRIBUTE] = format(server_span_id, "016x")
            if server_span_id is not None and server_span_id in self.span_processor.pending:
                buffer = self.pending.setdefault(server_span_id, [])
                if len(buffer) < MAX_BUFFERED_LOGS:
                    truncate_log_record(log_record)
                    buffer.append(log_record)
                else:
                    logger.debug("Apitally log buffer cap reached for request, dropping log record")
                return
            self.downstream.on_emit(log_record)
        except Exception:  # pragma: no cover
            logger.exception("Error in Apitally log record processor")

    def finish_request(self, server_span_id: int, keep: bool) -> None:
        buffer = self.pending.pop(server_span_id, None)
        if keep and buffer is not None:
            for log_record in buffer:
                self.downstream.on_emit(log_record)

    def shutdown(self) -> None:
        # Pending requests' SERVER spans can never export after shutdown, so their records are unreachable
        self.pending.clear()
        self.downstream.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.downstream.force_flush(timeout_millis)


def truncate_log_record(record: ReadableLogRecord | ReadWriteLogRecord) -> None:
    if record.instrumentation_scope is not None and record.instrumentation_scope.name == "apitally":
        return
    log_record = record.log_record
    if isinstance(log_record.body, str) and len(log_record.body) > MAX_LOG_VALUE_LENGTH:
        log_record.body = log_record.body[:MAX_LOG_VALUE_LENGTH]
    if log_record.attributes:
        oversized = [
            (key, value)
            for key, value in log_record.attributes.items()
            if isinstance(value, str) and len(value) > MAX_LOG_VALUE_LENGTH
        ]
        attributes = cast(MutableMapping[str, AnyValue], log_record.attributes)
        for key, value in oversized:
            attributes[key] = value[:MAX_LOG_VALUE_LENGTH]
