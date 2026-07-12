from __future__ import annotations

import gzip
import logging
import os
import tempfile
import threading
import time
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from typing import IO, cast


logger = logging.getLogger(__name__)

SIGNALS = ("traces", "logs", "metrics")

ROTATE_AT_UNCOMPRESSED_SIZE = 4_000_000
MAX_SPOOL_SIZE_DISK = 50_000_000
MAX_SPOOL_SIZE_MEMORY = 10_000_000
# A file whose first send attempt might have been published server-side must not be re-sent
# after the server's 1h dedup window; one minute of margin covers transit time and timeouts
SEND_HORIZON = 59 * 60
ORPHAN_MAX_AGE = 2 * 60 * 60


class DuplicateLogFilter(logging.Filter):
    """Drops repeats of the same message within a time window, so a persistently failing
    export path cannot log endlessly."""

    def __init__(self, window_seconds: float = 60.0) -> None:
        super().__init__()
        self.window_seconds = window_seconds
        self.last_logged: dict[tuple[str, int, str], float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        # Keyed on the rendered message, not the template, so warnings that differ only
        # in their arguments (e.g. per-signal data loss) are not swallowed
        key = (record.module, record.levelno, record.getMessage())
        now = time.time()
        last = self.last_logged.get(key)
        if last is not None and now - last < self.window_seconds:
            return False
        self.last_logged[key] = now
        return True


duplicate_log_filter = DuplicateLogFilter()
logger.addFilter(duplicate_log_filter)


class SpoolFile:
    """One gzip stream of concatenated OTLP request payloads for a single signal, written
    over a temp file or, as fallback, an in-memory buffer."""

    def __init__(self, signal: str, in_memory: bool) -> None:
        self.signal = signal
        self.created_at = time.time()
        self.first_attempt_at: float | None = None
        self.uncompressed_size = 0
        self.compressed_size = 0
        self.path: Path | None = None
        if in_memory:
            self.sink: IO[bytes] = BytesIO()
        else:
            file = tempfile.NamedTemporaryFile(prefix="apitally-", suffix=".gz", delete=False)
            self.sink = cast("IO[bytes]", file)
            self.path = Path(file.name)
        self.gzip_stream = gzip.GzipFile(fileobj=self.sink, mode="wb")

    def write(self, payload: bytes) -> None:
        self.gzip_stream.write(payload)
        self.uncompressed_size += len(payload)

    def close(self) -> None:
        # The sink stays open so the bytes remain readable even if another process sweeps
        # the path. The flush moves the compressed bytes out of the Python-level buffer
        # onto disk; without it, a closed file shares its unwritten tail with a forked
        # child through the inherited file descriptor, and both processes flushing it
        # corrupts the file
        self.gzip_stream.close()
        self.sink.flush()
        self.compressed_size = self.sink.tell()

    def read_bytes(self) -> bytes:
        self.sink.seek(0)
        return self.sink.read()

    def mark_attempt(self) -> None:
        # Monotonic clock: a wall-clock step backwards must not extend retries past the
        # server's dedup window
        if self.first_attempt_at is None:
            self.first_attempt_at = time.monotonic()

    def expired(self) -> bool:
        return self.first_attempt_at is not None and time.monotonic() - self.first_attempt_at > SEND_HORIZON

    def touch(self) -> None:
        if self.path is not None:
            with suppress(OSError):
                os.utime(self.path)

    def delete(self) -> None:
        with suppress(OSError, ValueError):
            self.gzip_stream.close()
        with suppress(OSError):
            self.sink.close()
        if self.path is not None:
            with suppress(OSError):
                self.path.unlink(missing_ok=True)


class Spool:
    """Byte spool between the batch processors and the export worker: accepts serialized
    payloads per signal, manages file rotation, caps and eviction. Thread-safe; appends
    come from the batch worker threads, rotation and sending from the export worker."""

    def __init__(self) -> None:
        self.in_memory = not check_writable_fs()
        if self.in_memory:
            logger.warning(
                "Unable to create temporary files, buffering telemetry in memory (max %d MB)",
                MAX_SPOOL_SIZE_MEMORY // 1_000_000,
            )
        else:
            cleanup_orphaned_files()
        self.max_size = MAX_SPOOL_SIZE_MEMORY if self.in_memory else MAX_SPOOL_SIZE_DISK
        self.lock = threading.Lock()
        self.current: dict[str, SpoolFile] = {}
        self.closed: list[SpoolFile] = []

    def append(self, signal: str, payload: bytes) -> None:
        with self.lock:
            current = self.current.get(signal)
            if current is not None and current.uncompressed_size + len(payload) > ROTATE_AT_UNCOMPRESSED_SIZE:
                self._rotate_locked(signal)
                current = None
            try:
                if current is None:
                    current = SpoolFile(signal, self.in_memory)
                    self.current[signal] = current
                current.write(payload)
            except OSError:
                logger.warning("Error writing telemetry to disk, dropping buffered %s", signal, exc_info=True)
                self._discard_current_locked(signal)
            self._evict_locked()

    def rotate_for_export(self) -> None:
        """Close each signal's current file so it becomes sendable, unless the signal
        already has closed files waiting (during a backlog the current file keeps growing
        instead of adding one small file per cycle)."""
        with self.lock:
            for signal in SIGNALS:
                if signal in self.current and not any(file.signal == signal for file in self.closed):
                    self._rotate_locked(signal)
            self._evict_locked()

    def close_current_files(self) -> None:
        """Close every signal's current file so no gzip writer is open across a fork."""
        with self.lock:
            for signal in list(self.current):
                self._rotate_locked(signal)

    def pending_files(self) -> list[SpoolFile]:
        """Closed files in send order (oldest first)."""
        with self.lock:
            return list(self.closed)

    def delete_file(self, file: SpoolFile) -> None:
        with self.lock:
            if file in self.closed:
                self.closed.remove(file)
            file.delete()

    def touch_files(self) -> None:
        """Refresh mtimes so sibling processes' orphan sweeps never touch live files."""
        with self.lock:
            for file in (*self.current.values(), *self.closed):
                file.touch()

    def clear(self) -> None:
        """Teardown for tests and reset. The spool never deletes files from a finalizer,
        so a forked child can safely abandon an inherited instance."""
        with self.lock:
            for signal in list(self.current):
                self._discard_current_locked(signal)
            for file in self.closed:
                file.delete()
            self.closed.clear()

    def _rotate_locked(self, signal: str) -> None:
        current = self.current.pop(signal, None)
        if current is None:
            return
        try:
            current.close()
        except OSError:
            logger.warning("Error writing telemetry to disk, dropping buffered %s", signal, exc_info=True)
            current.delete()
            return
        self.closed.append(current)

    def _discard_current_locked(self, signal: str) -> None:
        current = self.current.pop(signal, None)
        if current is not None:
            current.delete()

    def _evict_locked(self) -> None:
        for file in [file for file in self.closed if file.expired()]:
            logger.warning("Buffered %s could not be delivered within an hour and was dropped", file.signal)
            self.closed.remove(file)
            file.delete()
        while self._total_size_locked() > self.max_size:
            # Metrics files are exempt: a full hour of metric payloads is small, and the
            # aggregate data is the primary value that must survive an outage
            oldest = next((file for file in self.closed if file.signal != "metrics"), None)
            if oldest is None:
                break
            logger.warning("Apitally buffer size limit reached, dropping oldest buffered %s", oldest.signal)
            self.closed.remove(oldest)
            oldest.delete()

    def _total_size_locked(self) -> int:
        return sum(file.compressed_size for file in self.closed) + sum(
            file.sink.tell() for file in self.current.values()
        )


def check_writable_fs() -> bool:
    try:
        with tempfile.NamedTemporaryFile(prefix="apitally-"):
            return True
    except OSError:
        return False


def cleanup_orphaned_files() -> None:
    """Best-effort removal of spool files left behind by crashed processes. Live processes
    shield their files by touching them every export cycle."""
    cutoff = time.time() - ORPHAN_MAX_AGE
    with suppress(OSError):
        for path in Path(tempfile.gettempdir()).glob("apitally-*.gz"):
            with suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
