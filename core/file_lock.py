"""A small cross-process advisory lock, built on O_EXCL.

Atomic writes stop a reader from seeing half a file. They do nothing about two
writers who both read the same document, each apply their own change, and each
write the whole thing back — the second one silently erases the first. That is
B-012, and the only fix is to make read-modify-write one operation that another
process cannot start in the middle of.

Deliberately plain: a lock file created with ``O_CREAT|O_EXCL`` (atomic on
POSIX and on Windows), holding the owner's pid and start time so a human can
tell who is holding it. No fcntl, no msvcrt — those differ per platform and
this has to work identically on Luis's Windows box and on a Linux server.

A crashed holder would otherwise wedge the file forever, so a lock older than
``stale_after`` is broken and taken. That is a real (small) risk of two writers
overlapping, accepted in exchange for never needing a human to delete a file to
un-wedge settings. Keep ``stale_after`` comfortably longer than any write.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
DEFAULT_STALE_AFTER = 30.0
_POLL_SECONDS = 0.02


class LockTimeout(TimeoutError):
    """Nobody released the lock in time. The caller did NOT get it."""


class FileLock:
    """Context manager. ``with FileLock(path): ...`` holds it for the block."""

    def __init__(self, path: str, *, timeout: float = DEFAULT_TIMEOUT,
                 stale_after: float = DEFAULT_STALE_AFTER) -> None:
        self.path = str(path)
        self.timeout = max(0.0, float(timeout))
        self.stale_after = max(1.0, float(stale_after))
        self._held = False

    # -- internals ---------------------------------------------------------

    def _try_acquire(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                return False
            raise
        try:
            os.write(fd, json.dumps({
                "pid": os.getpid(),
                "acquired_at": time.time(),
            }).encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def _age(self) -> Optional[float]:
        try:
            return max(0.0, time.time() - os.path.getmtime(self.path))
        except OSError:
            return None

    def _break_if_stale(self) -> None:
        age = self._age()
        if age is None or age < self.stale_after:
            return
        try:
            os.replace(self.path, self.path + ".stale")
            os.remove(self.path + ".stale")
            logger.warning("file lock %s was stale after %.0fs — taken over",
                           self.path, age)
        except OSError:
            pass  # somebody else got there first; the next attempt will tell

    # -- api ---------------------------------------------------------------

    def acquire(self) -> "FileLock":
        deadline = time.monotonic() + self.timeout
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        while True:
            if self._try_acquire():
                self._held = True
                return self
            self._break_if_stale()
            if time.monotonic() >= deadline:
                raise LockTimeout(
                    f"could not acquire {self.path} within {self.timeout:.1f}s "
                    f"(held for {self._age()}s)"
                )
            time.sleep(_POLL_SECONDS)

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            os.remove(self.path)
        except OSError:
            pass

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc) -> bool:
        self.release()
        return False
