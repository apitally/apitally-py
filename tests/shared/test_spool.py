import gzip
import logging
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

from apitally.shared import spool as spool_module
from apitally.shared.spool import (
    MAX_RETRY_TIME_AFTER_FIRST_ATTEMPT,
    MAX_UNCOMPRESSED_FILE_SIZE,
    Spool,
    SpoolFile,
    cleanup_orphaned_files,
)
from tests.conftest import read_spool_file


@pytest.fixture(params=["disk", "memory"])
def spool(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[Spool]:
    if request.param == "memory":
        monkeypatch.setattr(spool_module, "is_temp_dir_writable", lambda: False)
    spool = Spool()
    yield spool
    spool.clear()


def serialized_trace_request(span_name: str) -> bytes:
    return ExportTraceServiceRequest(
        resource_spans=[ResourceSpans(scope_spans=[ScopeSpans(spans=[Span(name=span_name)])])]
    ).SerializeToString()


def parse_file(file: SpoolFile) -> ExportTraceServiceRequest:
    return ExportTraceServiceRequest.FromString(gzip.decompress(read_spool_file(file)))


def test_concatenated_payloads_parse_as_one_merged_request(spool: Spool) -> None:
    spool.append("traces", serialized_trace_request("first"))
    spool.append("traces", serialized_trace_request("second"))
    spool.rotate_for_export()
    (file,) = spool.pending_files()
    request = parse_file(file)
    assert len(request.resource_spans) == 2
    span_names = {span.name for rs in request.resource_spans for ss in rs.scope_spans for span in ss.spans}
    assert span_names == {"first", "second"}


def test_closed_file_is_fully_flushed_to_disk() -> None:
    spool = Spool()
    try:
        spool.append("traces", b"payload")
        spool.rotate_for_export()
        (file,) = spool.pending_files()
        assert file.path is not None
        assert file.compressed_size > 0
        assert os.stat(file.path).st_size == file.compressed_size
        assert gzip.decompress(file.path.read_bytes()) == b"payload"
    finally:
        spool.clear()


def test_rotation_error_discards_current_file_and_recovers(
    spool: Spool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spool.append("traces", b"first")
    failed_file = spool.current["traces"]

    def raise_oserror(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(failed_file.gzip_stream, "close", raise_oserror)
    with caplog.at_level(logging.WARNING, logger="apitally.shared.spool"):
        spool.rotate_for_export()
    assert len(caplog.records) == 1
    assert "traces" not in spool.current
    assert spool.pending_files() == []
    if failed_file.path is not None:
        assert not failed_file.path.exists()
    spool.append("traces", b"second")
    spool.rotate_for_export()
    (file,) = spool.pending_files()
    assert gzip.decompress(read_spool_file(file)) == b"second"


def test_append_rotates_before_crossing_size_threshold(spool: Spool) -> None:
    spool.append("traces", b"a" * 3_000_000)
    spool.append("traces", b"b" * 2_000_000)
    (file,) = spool.pending_files()
    assert file.uncompressed_size <= MAX_UNCOMPRESSED_FILE_SIZE
    assert gzip.decompress(read_spool_file(file)) == b"a" * 3_000_000
    assert spool.current["traces"].uncompressed_size == 2_000_000


def test_attempted_file_expires_after_max_retry_time(spool: Spool, caplog: pytest.LogCaptureFixture) -> None:
    for signal in ("traces", "metrics", "logs"):
        spool.append(signal, b"payload")
    spool.rotate_for_export()
    files = {file.signal: file for file in spool.pending_files()}
    for signal in ("traces", "metrics"):
        files[signal].first_attempt_at = time.monotonic() - MAX_RETRY_TIME_AFTER_FIRST_ATTEMPT - 1
    with caplog.at_level(logging.WARNING, logger="apitally.shared.spool"):
        spool.rotate_for_export()
    assert [file.signal for file in spool.pending_files()] == ["logs"]
    for signal in ("traces", "metrics"):
        if (path := files[signal].path) is not None:
            assert not path.exists()
    eviction_warnings = [record.getMessage() for record in caplog.records if "dropped" in record.message]
    assert len(eviction_warnings) == 2
    assert any("traces" in message for message in eviction_warnings)
    assert any("metrics" in message for message in eviction_warnings)


def test_size_cap_evicts_oldest_non_metrics_first(spool: Spool) -> None:
    spool.append("metrics", os.urandom(10_000))
    spool.rotate_for_export()
    spool.append("traces", os.urandom(10_000))
    spool.append("logs", os.urandom(10_000))
    spool.rotate_for_export()
    files = spool.pending_files()
    assert [file.signal for file in files] == ["metrics", "traces", "logs"]
    spool.max_size = sum(file.compressed_size for file in files) - 1
    spool.rotate_for_export()
    assert [file.signal for file in spool.pending_files()] == ["metrics", "logs"]


def test_unwritable_temp_dir_falls_back_to_memory(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(spool_module, "is_temp_dir_writable", lambda: False)
    with caplog.at_level(logging.WARNING, logger="apitally.shared.spool"):
        spool = Spool()
    assert spool.in_memory
    assert len(caplog.records) == 1
    assert "memory" in caplog.records[0].message
    spool.append("traces", b"payload")
    spool.rotate_for_export()
    (file,) = spool.pending_files()
    assert file.path is None
    assert gzip.decompress(read_spool_file(file)) == b"payload"
    spool.clear()


def test_append_error_discards_current_file_and_recovers(
    spool: Spool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spool.append("traces", b"first")
    failed_file = spool.current["traces"]

    def raise_oserror(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(failed_file.gzip_stream, "write", raise_oserror)
    with caplog.at_level(logging.WARNING, logger="apitally.shared.spool"):
        spool.append("traces", b"second")
    assert len(caplog.records) == 1
    assert "traces" not in spool.current
    if failed_file.path is not None:
        assert not failed_file.path.exists()
    spool.append("traces", b"third")
    spool.rotate_for_export()
    (file,) = spool.pending_files()
    assert gzip.decompress(read_spool_file(file)) == b"third"


def test_orphan_cleanup_removes_untouched_stale_files_only() -> None:
    orphan = tempfile.NamedTemporaryFile(prefix="apitally-", suffix=".gz", delete=False)
    orphan.close()
    spool = Spool()
    try:
        spool.append("traces", b"first")
        spool.rotate_for_export()
        spool.append("traces", b"second")
        spool_paths = [file.path for file in (*spool.pending_files(), spool.current["traces"])]
        old_time = time.time() - 3 * 60 * 60
        for path in (orphan.name, *spool_paths):
            assert path is not None
            os.utime(path, (old_time, old_time))
        spool.touch_files()
        cleanup_orphaned_files()
        assert not Path(orphan.name).exists()
        for path in spool_paths:
            assert path is not None and path.exists()
    finally:
        Path(orphan.name).unlink(missing_ok=True)
        spool.clear()
