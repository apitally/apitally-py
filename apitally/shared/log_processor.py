from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import cast

from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor, ReadWriteLogRecord
from opentelemetry.util.types import AnyValue

from apitally.shared.config import ApitallyConfig, get_config
from apitally.shared.span_processor import ApitallySpanProcessor


logger = logging.getLogger(__name__)

SERVER_SPAN_ID_ATTRIBUTE = "apitally.request.server_span_id"
SELF_LOGGER_NAMESPACES = ("apitally", "opentelemetry")
MAX_BUFFERED_LOGS = 1_000

installed_handler: LoggingHandler | None = None


def install_root_handler(logger_provider: LoggerProvider) -> LoggingHandler | None:
    """Bridge stdlib logging into the private LoggerProvider."""
    global installed_handler
    config = get_config() or ApitallyConfig()
    if not config.capture_logs:
        return None
    if installed_handler is None:
        handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider, log_code_attributes=True)
        handler.addFilter(exclude_self_logs)
        logging.getLogger().addHandler(handler)
        installed_handler = handler
    return installed_handler


def uninstall_root_handler() -> None:
    global installed_handler
    if installed_handler is not None:
        logging.getLogger().removeHandler(installed_handler)
        installed_handler = None


def exclude_self_logs(record: logging.LogRecord) -> bool:
    # SDK and OTel self-logs stay out of the export; they still reach the user's own handlers
    return record.name.partition(".")[0] not in SELF_LOGGER_NAMESPACES


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
                # Scope "apitally" passes without request context to preserve the startup event (spec section 9)
                if log_record.instrumentation_scope is None or log_record.instrumentation_scope.name != "apitally":
                    return
            elif record.attributes is not None:
                # ReadWriteLogRecord.__post_init__ replaces attributes with mutable BoundedAttributes
                attributes = cast("MutableMapping[str, AnyValue]", record.attributes)
                attributes[SERVER_SPAN_ID_ATTRIBUTE] = format(server_span_id, "016x")
            if server_span_id is not None and server_span_id in self.span_processor.pending:
                buffer = self.pending.setdefault(server_span_id, [])
                if len(buffer) < MAX_BUFFERED_LOGS:
                    buffer.append(log_record)
                else:
                    logger.debug("Apitally log buffer cap reached for request, dropping log record")
                return
            self.downstream.on_emit(log_record)
        except Exception:
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
