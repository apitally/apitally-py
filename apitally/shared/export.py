import atexit
import logging
import random
import threading
from collections.abc import MutableMapping, Sequence
from typing import Any, cast

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
from apitally.shared.log_processor import ApitallyLogRecordProcessor
from apitally.shared.providers import DISTRO_VERSION, endpoint_url, export_headers
from apitally.shared.span_processor import ApitallySpanProcessor
from apitally.shared.spool import Spool, SpoolFile


logger = logging.getLogger(__name__)

# Small enough that no single append can overshoot the spool's rotation threshold
ENCODE_CHUNK_SIZE = 32

MAX_LOG_VALUE_LENGTH = 2048

BATCH_SCHEDULE_DELAY_MILLIS = 1_000
BATCH_MAX_QUEUE_SIZE = 2_048
BATCH_MAX_EXPORT_BATCH_SIZE = 512
BATCH_EXPORT_TIMEOUT_MILLIS = 30_000

DEFAULT_EXPORT_INTERVAL = 15.0
INITIAL_EXPORT_DELAY = 2.0
EXPORT_INTERVAL_HEADER = "Apitally-Export-Interval"
MIN_EXPORT_INTERVAL = 5
MAX_EXPORT_INTERVAL = 60
REQUEST_TIMEOUT = 10
MAX_SENDS_PER_CYCLE = 10
RETRYABLE_STATUS_CODES = frozenset({408, 429})


class SpoolSpanExporter(SpanExporter):
    """Encodes drained span batches to protobuf and appends them to the spool."""

    def __init__(self, spool: Spool) -> None:
        self.spool = spool

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for chunk in chunked(spans):
            self.spool.append("traces", encode_spans(chunk).SerializeToString())
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        # The spool's lifecycle is owned by activation
        pass


class SpoolLogExporter(LogRecordExporter):
    """Truncates oversized log records, encodes to protobuf and appends them to the spool."""

    def __init__(self, spool: Spool) -> None:
        self.spool = spool

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        for record in batch:
            truncate_log_record(record)
        for chunk in chunked(batch):
            self.spool.append("logs", encode_logs(chunk).SerializeToString())
        return LogRecordExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def shutdown(self) -> None:
        pass


class ExportWorker:
    """Background thread sending spool files to the OTLP endpoint every export interval."""

    def __init__(
        self,
        spool: Spool,
        span_processor: ApitallySpanProcessor,
        log_processor: ApitallyLogRecordProcessor,
        env: str,
        proxy_urls: dict[str, str] | None = None,
    ) -> None:
        self.spool = spool
        self.span_processor = span_processor
        self.log_processor = log_processor
        self.interval: float = DEFAULT_EXPORT_INTERVAL
        self.session = requests.Session()
        # Environment lookups per request would call macOS's _scproxy in forked workers, which crashes the process
        self.session.trust_env = False
        if proxy_urls:
            self.session.proxies.update(proxy_urls)
        self.headers = {
            **export_headers(env),
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "gzip",
            "User-Agent": f"apitally-py/{DISTRO_VERSION}",
        }
        self.warned_statuses: set[int] = set()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.random = random.Random()

    def start(self) -> None:
        # A thread whose stop event is set is winding down (join timed out) and must not block the restart
        if self.thread is not None and self.thread.is_alive() and not self.stop_event.is_set():  # pragma: no cover
            return
        # Fresh Event per thread: a thread that outlived its join timeout exits on its own set event
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
        """Stop the thread and attempt one final unpaced drain-and-send pass."""
        self.stop()
        try:
            self.run_cycle(None, final=True)
        except Exception:  # pragma: no cover
            logger.debug("Error in final Apitally export on shutdown", exc_info=True)

    def run(self, stop_event: threading.Event) -> None:
        delay = INITIAL_EXPORT_DELAY
        while not stop_event.wait(delay):
            try:
                self.run_cycle(stop_event)
            except Exception:  # pragma: no cover
                logger.debug("Error in Apitally export cycle", exc_info=True)
            # Jitter desynchronizes deployments whose processes started together
            delay = self.interval * self.random.uniform(0.9, 1.1)

    def run_cycle(self, stop_event: threading.Event | None, final: bool = False) -> None:
        # Suppress instrumentation so our own flushes and POSTs generate no telemetry
        token = otel_context.attach(otel_context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            self.span_processor.downstream.force_flush()
            self.log_processor.downstream.force_flush()
            if metrics.reader is not None:
                metrics.reader.collect()
            if final:
                self.spool.close_current_files()
            else:
                self.spool.rotate_for_export()
                self.spool.touch_files()
            self.send_pending(stop_event, cap=not final)
        finally:
            otel_context.detach(token)

    def send_pending(self, stop_event: threading.Event | None, cap: bool = True) -> None:
        """During an outage this amounts to one probe POST per cycle. The final drain on
        shutdown passes no stop event and runs unpaced and uncapped."""
        sent = 0
        for file in self.spool.pending_files():
            if (cap and sent >= MAX_SENDS_PER_CYCLE) or (stop_event is not None and stop_event.is_set()):
                return
            if sent > 0 and stop_event is not None and stop_event.wait(self.random.uniform(0.1, 0.5)):
                return
            if file.is_expired():
                # A retry landing outside the server's dedup window could double-ingest
                logger.warning("Buffered %s could not be delivered within an hour and was dropped", file.signal)
                self.spool.delete_file(file)
                continue
            sent += 1
            if not self.send_file(file):
                return

    def send_file(self, file: SpoolFile) -> bool:
        """Streams the file's bytes verbatim. Returns False on a retryable failure, which
        ends the cycle and keeps the file queued."""
        file.mark_attempt()
        url = endpoint_url(f"/v1/{file.signal}")
        try:
            try:
                file.sink.seek(0)
                response = self.session.post(url, data=file.sink, headers=self.headers, timeout=REQUEST_TIMEOUT)
            except requests.ConnectionError:
                # The server may close an idle keep-alive connection mid-request; retry once
                file.sink.seek(0)
                response = self.session.post(url, data=file.sink, headers=self.headers, timeout=REQUEST_TIMEOUT)
        except (requests.ConnectionError, requests.Timeout):
            logger.debug("Sending buffered %s to Apitally failed with a connection error, will retry", file.signal)
            return False
        except (OSError, ValueError):
            logger.warning("Error reading buffered %s, dropping it", file.signal, exc_info=True)
            self.spool.delete_file(file)
            return True
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


def create_span_exporter(spool: Spool) -> SpanExporter:
    return SpoolSpanExporter(spool)


def create_log_exporter(spool: Spool) -> LogRecordExporter:
    return SpoolLogExporter(spool)


def resolve_proxy_urls() -> dict[str, str]:
    proxy_urls = requests.utils.get_environ_proxies(endpoint_url("/"))
    return {str(key): str(value) for key, value in cast(dict[Any, Any], proxy_urls).items()}


def chunked(batch: Sequence) -> list[Sequence]:
    return [batch[i : i + ENCODE_CHUNK_SIZE] for i in range(0, len(batch), ENCODE_CHUNK_SIZE)]


def truncate_log_record(record: ReadableLogRecord) -> None:
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
