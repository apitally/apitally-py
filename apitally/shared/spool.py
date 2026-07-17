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

MAX_UNCOMPRESSED_FILE_SIZE = 4_000_000
MAX_SPOOL_SIZE_DISK = 50_000_000
MAX_SPOOL_SIZE_MEMORY = 10_000_000
MAX_RETRY_TIME_AFTER_FIRST_ATTEMPT = 59 * 60
MAX_UNTOUCHED_FILE_AGE = 2 * 60 * 60


class SpoolFile:
    """One gzip stream of concatenated OTLP request payloads for a single signal, written
    over a temp file or, as fallback, an in-memory buffer."""

    def __init__(self, signal: str, in_memory: bool) -> None:
        self.signal = signal
        self.first_attempt_at: float | None = None
        self.uncompressed_size = 0
        self.compressed_size = 0
        self.path: Path | None = None
        if in_memory:
            self.sink: IO[bytes] = BytesIO()
        else:
            file = tempfile.NamedTemporaryFile(prefix="apitally-", suffix=".gz", delete=False)
            self.sink = cast(IO[bytes], file)
            self.path = Path(file.name)
        self.gzip_stream = gzip.GzipFile(fileobj=self.sink, mode="wb")

    def write(self, payload: bytes) -> None:
        self.gzip_stream.write(payload)
        self.uncompressed_size += len(payload)

    def close(self) -> None:
        self.gzip_stream.close()
        self.sink.flush()
        self.compressed_size = self.sink.tell()

    def mark_attempt(self) -> None:
        if self.first_attempt_at is None:
            self.first_attempt_at = time.monotonic()

    def is_expired(self) -> bool:
        return (
            self.first_attempt_at is not None
            and time.monotonic() - self.first_attempt_at > MAX_RETRY_TIME_AFTER_FIRST_ATTEMPT
        )

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
    """Byte spool between the batch processors and the export worker. Thread-safe: appends
    come from the batch worker threads, rotation and sending from the export worker."""

    def __init__(self) -> None:
        self.in_memory = not is_temp_dir_writable()
        if self.in_memory:
            logger.warning(
                "Unable to create temporary files, buffering telemetry in memory (max %d MB)",
                MAX_SPOOL_SIZE_MEMORY // 1_000_000,
            )
        else:
            cleanup_orphaned_files()
        self.max_size = MAX_SPOOL_SIZE_MEMORY if self.in_memory else MAX_SPOOL_SIZE_DISK
        self.write_error_logged = False
        self.lock = threading.Lock()
        self.current: dict[str, SpoolFile] = {}
        self.closed: list[SpoolFile] = []

    def append(self, signal: str, payload: bytes) -> None:
        with self.lock:
            current = self.current.get(signal)
            if current is not None and current.uncompressed_size + len(payload) > MAX_UNCOMPRESSED_FILE_SIZE:
                self.rotate_locked(signal)
                current = None
            try:
                if current is None:
                    current = SpoolFile(signal, self.in_memory)
                    self.current[signal] = current
                current.write(payload)
                self.write_error_logged = False
            except OSError:
                if not self.write_error_logged:
                    self.write_error_logged = True
                    logger.warning("Error writing telemetry to disk, dropping buffered %s", signal, exc_info=True)
                self.discard_current_locked(signal)
            self.evict_locked()

    def rotate_for_export(self) -> None:
        """Close each signal's current file so it becomes sendable, unless closed files are
        already waiting (a backlog grows the current file instead of adding one per cycle)."""
        with self.lock:
            for signal in SIGNALS:
                if signal in self.current and not any(file.signal == signal for file in self.closed):
                    self.rotate_locked(signal)
            self.evict_locked()

    def close_current_files(self) -> None:
        with self.lock:
            for signal in list(self.current):
                self.rotate_locked(signal)

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
        """Refresh mtimes so cleanup_orphaned_files in a sibling process never removes live files."""
        with self.lock:
            for file in (*self.current.values(), *self.closed):
                file.touch()

    def clear(self) -> None:
        """Teardown for tests and reset."""
        with self.lock:
            for signal in list(self.current):
                self.discard_current_locked(signal)
            for file in self.closed:
                file.delete()
            self.closed.clear()

    def rotate_locked(self, signal: str) -> None:
        current = self.current.pop(signal)
        try:
            current.close()
        except OSError:
            if not self.write_error_logged:
                self.write_error_logged = True
                logger.warning("Error writing telemetry to disk, dropping buffered %s", signal, exc_info=True)
            current.delete()
            return
        self.write_error_logged = False
        self.closed.append(current)

    def discard_current_locked(self, signal: str) -> None:
        current = self.current.pop(signal, None)
        if current is not None:
            current.delete()

    def evict_locked(self) -> None:
        for file in [file for file in self.closed if file.is_expired()]:
            logger.warning("Buffered %s could not be delivered within an hour and was dropped", file.signal)
            self.closed.remove(file)
            file.delete()
        while self.total_size_locked() > self.max_size:
            # Prefer retaining metrics, but the size bound still applies when only metrics remain
            oldest = next((file for file in self.closed if file.signal != "metrics"), None)
            if oldest is None and self.closed:
                oldest = self.closed[0]
            if oldest is None:
                break
            logger.warning("Buffer size limit reached, dropping oldest buffered %s", oldest.signal)
            self.closed.remove(oldest)
            oldest.delete()

    def total_size_locked(self) -> int:
        return sum(file.compressed_size for file in self.closed) + sum(
            file.sink.tell() for file in self.current.values()
        )


def is_temp_dir_writable() -> bool:
    try:
        with tempfile.NamedTemporaryFile(prefix="apitally-"):
            return True
    except OSError:
        return False


def cleanup_orphaned_files() -> None:
    """Best-effort removal of spool files left behind by crashed processes. Live processes
    refresh their files' mtimes every export cycle, keeping them newer than the cutoff."""
    cutoff = time.time() - MAX_UNTOUCHED_FILE_AGE
    with suppress(OSError):
        for path in Path(tempfile.gettempdir()).glob("apitally-*.gz"):
            with suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
