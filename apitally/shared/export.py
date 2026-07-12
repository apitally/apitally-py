from __future__ import annotations

import atexit
import logging
import random
import threading
from collections.abc import MutableMapping, Sequence
from typing import TYPE_CHECKING, cast

import requests
from opentelemetry import context as otel_context
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.util.types import AnyValue

from apitally.shared import metrics
from apitally.shared.providers import DISTRO_VERSION, endpoint_url, export_headers
from apitally.shared.spool import Spool, SpoolFile, duplicate_log_filter


if TYPE_CHECKING:
    from apitally.shared.log_processor import ApitallyLogRecordProcessor
    from apitally.shared.span_processor import ApitallySpanProcessor


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

DEFAULT_EXPORT_INTERVAL = 15.0
INITIAL_EXPORT_DELAY = 2.0
EXPORT_INTERVAL_HEADER = "Apitally-Export-Interval"
MIN_EXPORT_INTERVAL = 5
MAX_EXPORT_INTERVAL = 900
REQUEST_TIMEOUT = 10
MAX_SENDS_PER_CYCLE = 10
RETRYABLE_STATUS_CODES = frozenset({408, 429})


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


class ExportWorker:
    """Background thread that runs every export interval: flushes the batch processors
    into the spool, collects metrics, rotates spool files and sends them to the OTLP
    endpoint oldest-first. A failed file simply waits for the next cycle; the spool is
    the retry buffer, so there is no backoff.

    The worker stores no references to the batch processors or the metric reader; each
    cycle resolves them through the stable identities (the processor wrappers' downstream
    attributes and the metrics module state), which the fork handlers repoint to fresh
    instances after a fork."""

    def __init__(
        self,
        spool: Spool,
        span_processor: ApitallySpanProcessor,
        log_processor: ApitallyLogRecordProcessor,
        env: str,
    ) -> None:
        self.spool = spool
        self.span_processor = span_processor
        self.log_processor = log_processor
        self.interval: float = DEFAULT_EXPORT_INTERVAL
        self.session = requests.Session()
        self.headers = {
            **export_headers(env),
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "gzip",
            "User-Agent": f"apitally-py/{DISTRO_VERSION}",
        }
        self.warned_statuses: set[int] = set()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():  # pragma: no cover
            return
        # A fresh Event per thread: if a previous thread outlived its join timeout, it
        # keeps observing its own set event and exits instead of racing the new thread
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self.run, args=(self.stop_event,), name="ApitallyExportWorker", daemon=True
        )
        self.thread.start()
        atexit.register(self.shutdown)

    def stop(self, timeout: float = 5.0) -> None:
        atexit.unregister(self.shutdown)
        self.stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout)

    def shutdown(self) -> None:
        """Stop the thread, then a final drain: flush everything recorded so far, rotate,
        and attempt one unpaced send pass."""
        self.stop()
        try:
            self.run_cycle(None)
        except Exception:  # pragma: no cover
            logger.debug("Error in final Apitally export on shutdown", exc_info=True)

    def run(self, stop_event: threading.Event) -> None:
        delay = INITIAL_EXPORT_DELAY
        while not stop_event.wait(delay):
            try:
                self.run_cycle(stop_event)
            except Exception:
                logger.warning("Error in Apitally export cycle", exc_info=True)
            # Jitter desynchronizes fleets whose processes started together in a deploy
            delay = self.interval * random.uniform(0.9, 1.1)

    def run_cycle(self, stop_event: threading.Event | None) -> None:
        # The suppression key keeps our own flushes and POSTs from generating telemetry
        # that would feed back into the pipeline
        token = otel_context.attach(otel_context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            self.span_processor.downstream.force_flush()
            self.log_processor.downstream.force_flush()
            if metrics.reader is not None:
                metrics.reader.collect()
            self.spool.rotate_for_export()
            self.spool.touch_files()
            self.send_pending(stop_event)
        finally:
            otel_context.detach(token)

    def send_pending(self, stop_event: threading.Event | None) -> None:
        """Send closed files oldest-first, capped per cycle, with short jittered sleeps
        between sends. During an outage this amounts to one probe POST per cycle. The
        final drain on shutdown passes no stop event and runs unpaced."""
        sent = 0
        for file in self.spool.pending_files():
            if sent >= MAX_SENDS_PER_CYCLE or (stop_event is not None and stop_event.is_set()):
                return
            if file.expired():
                # Checked immediately before each POST: a retry landing outside the
                # server's dedup window could double-ingest
                logger.warning("Buffered %s could not be delivered within an hour and was dropped", file.signal)
                self.spool.delete_file(file)
                continue
            if sent > 0 and stop_event is not None and stop_event.wait(random.uniform(0.1, 0.5)):
                return
            sent += 1
            if not self.send_file(file):
                return

    def send_file(self, file: SpoolFile) -> bool:
        """Send one file's bytes verbatim. Returns False when the cycle should end
        because of a retryable failure; the file stays queued for the next cycle."""
        try:
            body = file.read_bytes()
        except (OSError, ValueError):
            # An unreadable file must not block the queue
            logger.warning("Error reading buffered %s, dropping it", file.signal, exc_info=True)
            self.spool.delete_file(file)
            return True
        file.mark_attempt()
        url = endpoint_url(f"/v1/{file.signal}")
        try:
            try:
                response = self.session.post(url, data=body, headers=self.headers, timeout=REQUEST_TIMEOUT)
            except requests.ConnectionError:
                # The server may close an idle keep-alive connection mid-request; one
                # immediate retry, like the stock OTLP exporter
                response = self.session.post(url, data=body, headers=self.headers, timeout=REQUEST_TIMEOUT)
        except (requests.ConnectionError, requests.Timeout):
            logger.debug("Sending buffered %s to Apitally failed with a connection error, will retry", file.signal)
            return False
        self.apply_interval_header(response)
        if 200 <= response.status_code < 300:
            self.spool.delete_file(file)
            return True
        if response.status_code in RETRYABLE_STATUS_CODES or response.status_code >= 500:
            logger.debug(
                "Sending buffered %s to Apitally failed with HTTP %d, will retry", file.signal, response.status_code
            )
            return False
        if response.status_code not in self.warned_statuses:
            self.warned_statuses.add(response.status_code)
            logger.warning("Apitally rejected buffered %s with HTTP %d, dropping it", file.signal, response.status_code)
        self.spool.delete_file(file)
        return True

    def apply_interval_header(self, response: requests.Response) -> None:
        value = response.headers.get(EXPORT_INTERVAL_HEADER)
        if value:
            try:
                seconds = int(value)
            except ValueError:
                return
            self.interval = float(min(max(seconds, MIN_EXPORT_INTERVAL), MAX_EXPORT_INTERVAL))


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
