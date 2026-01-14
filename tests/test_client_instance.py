from __future__ import annotations

import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
from pytest_mock import MockerFixture


@pytest.fixture(autouse=True)
def mock_lock_dir(tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("apitally.client.instance.LOCK_DIR", tmp_path)


def test_get_app_env_hash() -> None:
    from apitally.client.instance import _get_app_env_hash

    hash1 = _get_app_env_hash("client-1", "dev")
    hash2 = _get_app_env_hash("client-1", "prod")
    hash3 = _get_app_env_hash("client-2", "dev")
    hash4 = _get_app_env_hash("client-1", "dev")

    # Same inputs produce same hash
    assert hash1 == hash4

    # Different inputs produce different hashes
    assert hash1 != hash2
    assert hash1 != hash3

    # Hash is 8 characters (hex)
    assert len(hash1) == 8
    assert all(c in "0123456789abcdef" for c in hash1)


def test_validate_uuid() -> None:
    from apitally.client.instance import _validate_uuid

    # Valid UUIDs
    assert _validate_uuid("550e8400-e29b-41d4-a716-446655440000") == "550e8400-e29b-41d4-a716-446655440000"
    assert _validate_uuid("550E8400-E29B-41D4-A716-446655440000") == "550e8400-e29b-41d4-a716-446655440000"

    # Invalid UUIDs
    assert _validate_uuid("") is None
    assert _validate_uuid("not-a-uuid") is None
    assert _validate_uuid("550e8400-e29b-41d4-a716") is None
    assert _validate_uuid("550e8400e29b41d4a716446655440000") == "550e8400-e29b-41d4-a716-446655440000"


def test_get_or_create_instance_uuid_creates_new() -> None:
    from apitally.client.instance import LOCK_DIR, _get_app_env_hash, _validate_uuid, get_or_create_instance_uuid

    client_id, env = str(uuid4()), "test"
    uuid, fd = get_or_create_instance_uuid(client_id, env)

    assert fd is not None
    assert _validate_uuid(uuid) is not None

    # Verify lock file exists
    app_env_hash = _get_app_env_hash(client_id, env)
    lock_file = LOCK_DIR / f"instance_{app_env_hash}_0.lock"
    assert lock_file.exists()
    assert lock_file.read_text() == uuid

    os.close(fd)


def test_get_or_create_instance_uuid_reuses_existing() -> None:
    from apitally.client.instance import get_or_create_instance_uuid

    client_id, env = str(uuid4()), "test"

    # First call creates UUID
    uuid1, fd1 = get_or_create_instance_uuid(client_id, env)
    assert fd1 is not None
    os.close(fd1)

    # Second call reuses same UUID
    uuid2, fd2 = get_or_create_instance_uuid(client_id, env)
    assert fd2 is not None
    assert uuid1 == uuid2

    os.close(fd2)


def test_get_or_create_instance_uuid_different_envs() -> None:
    from apitally.client.instance import get_or_create_instance_uuid

    client_id = str(uuid4())
    uuid1, fd1 = get_or_create_instance_uuid(client_id, "env1")
    uuid2, fd2 = get_or_create_instance_uuid(client_id, "env2")

    assert fd1 is not None
    assert fd2 is not None
    assert uuid1 != uuid2

    os.close(fd1)
    os.close(fd2)


def test_get_or_create_instance_uuid_multiple_slots() -> None:
    from apitally.client.instance import LOCK_DIR, _get_app_env_hash, get_or_create_instance_uuid

    client_id, env = str(uuid4()), "test"

    # Acquire multiple slots by holding locks
    fds: list[int] = []
    uuids: list[str] = []

    for _ in range(3):
        uuid, fd = get_or_create_instance_uuid(client_id, env)
        assert fd is not None
        fds.append(fd)
        uuids.append(uuid)

    # All UUIDs should be different
    assert len(set(uuids)) == 3

    # Check lock files exist for slots 0, 1, 2
    app_env_hash = _get_app_env_hash(client_id, env)
    for i in range(3):
        lock_file = LOCK_DIR / f"instance_{app_env_hash}_{i}.lock"
        assert lock_file.exists()

    for fd in fds:
        os.close(fd)


def test_validate_lock_files_removes_invalid() -> None:
    from apitally.client.instance import LOCK_DIR, _get_app_env_hash, _validate_lock_files

    client_id, env = str(uuid4()), "test"
    app_env_hash = _get_app_env_hash(client_id, env)
    LOCK_DIR.mkdir(exist_ok=True)

    # Create lock file with invalid content
    invalid_file = LOCK_DIR / f"instance_{app_env_hash}_0.lock"
    invalid_file.write_text("not-a-valid-uuid")

    # Create lock file with valid content
    valid_uuid = "550e8400-e29b-41d4-a716-446655440000"
    valid_file = LOCK_DIR / f"instance_{app_env_hash}_1.lock"
    valid_file.write_text(valid_uuid)

    _validate_lock_files(app_env_hash)

    # Invalid file should be removed
    assert not invalid_file.exists()
    # Valid file should remain
    assert valid_file.exists()


def test_validate_lock_files_removes_duplicates() -> None:
    from apitally.client.instance import LOCK_DIR, _get_app_env_hash, _validate_lock_files

    client_id, env = str(uuid4()), "test"
    app_env_hash = _get_app_env_hash(client_id, env)
    LOCK_DIR.mkdir(exist_ok=True)

    # Create multiple files with same UUID
    duplicate_uuid = "550e8400-e29b-41d4-a716-446655440000"
    file1 = LOCK_DIR / f"instance_{app_env_hash}_0.lock"
    file2 = LOCK_DIR / f"instance_{app_env_hash}_1.lock"
    file1.write_text(duplicate_uuid)
    file2.write_text(duplicate_uuid)

    _validate_lock_files(app_env_hash)

    # First file (sorted order) should remain, second should be removed
    assert file1.exists()
    assert not file2.exists()


def test_validate_lock_files_removes_old_files() -> None:
    from apitally.client.instance import LOCK_DIR, MAX_LOCK_AGE_SECONDS, _get_app_env_hash, _validate_lock_files

    client_id, env = str(uuid4()), "test"
    app_env_hash = _get_app_env_hash(client_id, env)
    LOCK_DIR.mkdir(exist_ok=True)

    # Create lock file with valid content
    old_uuid = "550e8400-e29b-41d4-a716-446655440000"
    old_file = LOCK_DIR / f"instance_{app_env_hash}_0.lock"
    old_file.write_text(old_uuid)

    # Set mtime to 25 hours ago
    old_mtime = time.time() - MAX_LOCK_AGE_SECONDS - 3600
    os.utime(old_file, (old_mtime, old_mtime))

    # Create recent lock file
    new_uuid = "660e8400-e29b-41d4-a716-446655440000"
    new_file = LOCK_DIR / f"instance_{app_env_hash}_1.lock"
    new_file.write_text(new_uuid)

    _validate_lock_files(app_env_hash)

    # Old file should be removed, new file should remain
    assert not old_file.exists()
    assert new_file.exists()


def test_get_or_create_instance_uuid_handles_corrupted_file() -> None:
    from apitally.client.instance import LOCK_DIR, _get_app_env_hash, _validate_uuid, get_or_create_instance_uuid

    client_id, env = str(uuid4()), "test"
    app_env_hash = _get_app_env_hash(client_id, env)
    LOCK_DIR.mkdir(exist_ok=True)

    # Create corrupted lock file
    corrupted_file = LOCK_DIR / f"instance_{app_env_hash}_0.lock"
    corrupted_file.write_text("corrupted-content")

    # Should clean up and create new UUID
    uuid, fd = get_or_create_instance_uuid(client_id, env)
    assert fd is not None
    assert _validate_uuid(uuid) is not None

    os.close(fd)
