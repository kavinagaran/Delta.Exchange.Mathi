"""Single-writer enforcement for the data directory (ADR 0002).

ADR 0002 states the engine is the only writer of ``data/``.  That was a
convention until a second instance was started by hand against a live one:
on Windows it crashed mid-startup, and on POSIX it would have been worse —
``unlink`` succeeds on an open file, so the second process would have deleted
the running engine's active capture file while the first kept writing to an
orphaned inode, losing the data silently.

An OS advisory lock is used rather than a PID file because the kernel releases
it when the holder dies, so a crashed engine leaves no stale lock to clear.

The owning PID is written into the lock file for diagnostics.  On Windows the
locked byte range makes that content unreadable while the lock is held, so it
is only legible once the holder is gone — which is when an operator actually
needs it.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType


class DataDirectoryLocked(RuntimeError):
    """Another process already owns this data directory."""


class DataDirectoryLock:
    """Exclusive, non-blocking, auto-released on process exit."""

    def __init__(self, data_dir: Path, name: str = "engine.lock") -> None:
        self._path = data_dir / name
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock_exclusive(fd)
        except OSError as exc:
            os.close(fd)
            raise DataDirectoryLocked(
                f"another btc_trend_engine already owns {self._path.parent} "
                f"({exc.__class__.__name__}). Stop it before starting another; "
                "two writers corrupt the event store."
            ) from exc
        os.truncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "DataDirectoryLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.release()


if os.name == "nt":
    import msvcrt

    def _lock_exclusive(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock_exclusive(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
