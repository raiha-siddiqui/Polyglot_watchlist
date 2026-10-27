"""Portable cross-process workspace lock with a bounded wait.

Uses ``portalocker`` (``msvcrt`` on Windows, ``fcntl.flock`` on POSIX) on a
dedicated ``writer.lock`` file. Locks are exclusive for readers and writers:
Windows ``msvcrt`` has no shared mode without the optional pywin32 package, and
v2 reads are short. Each acquisition opens its own file handle, so two threads
in one process exclude each other exactly like two processes.

The lock is cooperative and advisory on POSIX: a process that ignores it can
still edit files. Never delete ``writer.lock`` to bypass a busy workspace.
The operating system releases the lock when a holder process exits or crashes.
"""

from __future__ import annotations

from contextlib import contextmanager
import math
from numbers import Real
import os
from pathlib import Path
import threading
import time
from typing import Iterator

from watchlist.review.contracts import (
    ReentrantCallError,
    ValidationError,
    WorkspaceBusyError,
    WorkspaceError,
)

LOCK_NAME = "writer.lock"
_POLL_SECONDS = 0.02
_THREAD_STATE = threading.local()


def _held_keys() -> set[str]:
    """Lock files held by the current thread (re-entrancy detection only)."""
    keys = getattr(_THREAD_STATE, "keys", None)
    if keys is None:
        keys = _THREAD_STATE.keys = set()
    return keys


def validated_timeout(value: object) -> float:
    """Accept finite nonnegative real seconds (booleans rejected) before any I/O."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValidationError("lock timeout must be finite nonnegative seconds")
    try:
        timeout = float(value)
    except (OverflowError, ValueError):
        raise ValidationError(
            "lock timeout must be finite nonnegative seconds"
        ) from None
    if not math.isfinite(timeout) or timeout < 0:
        raise ValidationError("lock timeout must be finite nonnegative seconds")
    return timeout


def create_lock_file(root: Path) -> Path:
    """Create the lock file exclusively; an existing file means a claimed workspace."""
    path = root / LOCK_NAME
    try:
        with path.open("xb"):
            pass
    except FileExistsError:
        raise WorkspaceError(
            "workspace lock already exists; workspace is not new"
        ) from None
    return path


@contextmanager
def workspace_lock(root: Path, *, timeout: float) -> Iterator[None]:
    """Hold the exclusive workspace lock, waiting at most ``timeout`` seconds.

    Zero tries exactly once. The deadline uses a monotonic clock, so wall-clock
    changes cannot extend it. It bounds waiting only, not the protected work.
    The lock file is opened read-only and never written, so holding the lock
    changes no file contents. A nested acquisition by the holding thread raises
    ``ReentrantCallError`` at once; a non-contention lock failure raises
    ``WorkspaceError`` at once; only contention is retried until the deadline.
    """
    import portalocker

    timeout = validated_timeout(timeout)
    path = Path(root) / LOCK_NAME
    if path.is_symlink() or not path.is_file():
        raise WorkspaceError("workspace lock file is missing or not a regular file")
    key = os.path.normcase(os.path.abspath(path))
    held = _held_keys()
    if key in held:
        # Waiting would only time out: this thread is the holder.
        raise ReentrantCallError(
            "this thread already holds the review workspace lock; callbacks such as "
            "context providers must not call back into the review service"
        )
    deadline = time.monotonic() + timeout
    flags = portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING
    with path.open("rb") as handle:
        while True:
            try:
                portalocker.lock(handle, flags)
                break
            except portalocker.AlreadyLocked:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkspaceBusyError(
                        "review workspace is busy; lock wait timed out",
                        details={"timeout_seconds": timeout},
                    ) from None
                time.sleep(min(_POLL_SECONDS, remaining))
            except portalocker.LockException as exc:
                # Not contention (e.g. ENOLCK, unsupported file system): waiting cannot help.
                raise WorkspaceError(
                    f"cannot lock the review workspace: {exc}"
                ) from None
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)
            portalocker.unlock(handle)
