from __future__ import annotations

import logging
from collections.abc import MutableMapping, Sequence
from typing import cast

from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.util.types import AnyValue

from apitally.shared.spool import Spool, duplicate_log_filter


logger = logging.getLogger(__name__)
logger.addFilter(duplicate_log_filter)

# Sub-batch size for encoding: with every per-record input capped, a chunk stays far below
# the spool's rotation threshold, so no single append can overshoot the file size bound
ENCODE_CHUNK_SIZE = 32

# The server caps log messages at this length; matches 0.x MAX_LOG_MSG_LENGTH
MAX_LOG_VALUE_LENGTH = 2048

# Batch processor parameters are always passed explicitly, because the stock constructors
# fall back to OTEL_BSP_* / OTEL_BLRP_* env vars for any parameter left as None and this
# private pipeline must not be tuned by env vars aimed at the user's own exporters
BATCH_SCHEDULE_DELAY_MILLIS = 1_000
BATCH_MAX_QUEUE_SIZE = 2_048
BATCH_MAX_EXPORT_BATCH_SIZE = 512
BATCH_EXPORT_TIMEOUT_MILLIS = 30_000


class SpoolSpanExporter(SpanExporter):
    """Terminal exporter behind the batch span processor: encodes drained batches to
    protobuf and appends them to the spool."""

    def __init__(self, spool: Spool) -> None:
        self.spool = spool

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for chunk in chunked(spans):
            self.spool.append("traces", encode_spans(chunk).SerializeToString())
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        # The spool's lifecycle is owned by activation
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class SpoolLogExporter(LogRecordExporter):
    """Terminal exporter behind the batch log record processor: truncates oversized
    records, encodes to protobuf and appends to the spool."""

    def __init__(self, spool: Spool) -> None:
        self.spool = spool

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        for record in batch:
            truncate_log_record(record)
        for chunk in chunked(batch):
            self.spool.append("logs", encode_logs(chunk).SerializeToString())
        return LogRecordExportResult.SUCCESS

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def chunked(batch: Sequence) -> list[Sequence]:
    return [batch[i : i + ENCODE_CHUNK_SIZE] for i in range(0, len(batch), ENCODE_CHUNK_SIZE)]


def truncate_log_record(record: ReadableLogRecord) -> None:
    """Cut oversized body strings and attribute values. Nothing upstream bounds a log
    record body, and a single unbounded record would break the spool's file size bound.
    Records from the SDK's own "apitally" scope (e.g. the startup event, whose JSON body
    may legitimately be long) are exempt; their sizes are SDK-controlled."""
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
        attributes = cast("MutableMapping[str, AnyValue]", log_record.attributes)
        for key, value in oversized:
            attributes[key] = value[:MAX_LOG_VALUE_LENGTH]
