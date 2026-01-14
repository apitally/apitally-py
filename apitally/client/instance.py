import hashlib
import os
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Optional, Union
from uuid import UUID, uuid4


LOCK_DIR = Path(tempfile.gettempdir()) / "apitally"
MAX_SLOTS = 100
MAX_LOCK_AGE_SECONDS = 24 * 60 * 60


def get_or_create_instance_uuid(client_id: str, env: str) -> tuple[str, Union[int, None]]:
    """
    Get or create a stable instance UUID using file-based locking.

    Uses a slot-based approach where each process acquires an exclusive lock on a
    slot file. This ensures:
    - Single process restarts reuse the same UUID (same slot)
    - Multiple workers get different UUIDs (different slots)
    - UUIDs persist across restarts and hot reloads

    Returns a tuple of (uuid, fd) where fd is the file descriptor holding the lock.
    The fd must be kept open to maintain the lock.
    """
    app_env_hash = _get_app_env_hash(client_id, env)

    try:
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover
        return str(uuid4()), None

    _validate_lock_files(app_env_hash)

    for slot in range(MAX_SLOTS):
        lock_file = LOCK_DIR / f"instance_{app_env_hash}_{slot}.lock"
        fd: Optional[int] = None
        try:
            fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)

            if not _try_acquire_lock(fd):
                # Failed to acquire lock, try next slot
                os.close(fd)
                continue

            os.lseek(fd, 0, os.SEEK_SET)
            content = os.read(fd, 64).decode().replace("\0", "").strip()
            valid_uuid = _validate_uuid(content)
            if valid_uuid is not None:
                return valid_uuid, fd

            new_uuid = str(uuid4())
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, new_uuid.encode())
            return new_uuid, fd

        except Exception:  # pragma: no cover
            if fd is not None:
                with suppress(OSError):
                    os.close(fd)

    # All slots taken, fall back to random UUID
    return str(uuid4()), None


def _get_app_env_hash(client_id: str, env: str) -> str:
    """Create a short hash to identify this client_id + env combination."""
    combined = f"{client_id}:{env}"
    return hashlib.sha256(combined.encode()).hexdigest()[:8]


def _validate_lock_files(app_env_hash: str) -> None:
    """Delete lock files with invalid UUIDs, duplicates, or older than 24 hours."""
    lock_files = sorted(LOCK_DIR.glob(f"instance_{app_env_hash}_*.lock"))
    seen_uuids: set[str] = set()
    now = time.time()

    for lock_file in lock_files:
        with suppress(Exception):
            if now - lock_file.stat().st_mtime > MAX_LOCK_AGE_SECONDS:
                lock_file.unlink(missing_ok=True)
                continue

            content = lock_file.read_text().replace("\0", "").strip()
            uuid = _validate_uuid(content)
            if uuid is None or uuid in seen_uuids:
                lock_file.unlink(missing_ok=True)
                continue

            seen_uuids.add(uuid)


def _validate_uuid(value: str) -> Optional[str]:
    try:
        uuid = UUID(value)
        return str(uuid)
    except ValueError:
        return None


def _try_acquire_lock(fd: int) -> bool:
    """Try to acquire an exclusive non-blocking lock on file descriptor."""
    try:
        if sys.platform == "win32":  # pragma: no cover
            import msvcrt

            # Ensure there's at least 1 byte to lock
            if os.lseek(fd, 0, os.SEEK_END) == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        return True
    except Exception:
        return False
