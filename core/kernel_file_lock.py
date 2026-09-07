"""Process-owned advisory locks; crashes release them, age never steals them.

The lock file is deliberately stable: unlinking it permits two callers to lock
different inodes for the same resource. Callers must keep it beside their data.
"""

from __future__ import annotations

import errno
import os
import time

from core.file_lock import LockTimeout


class KernelFileLock:
    def __init__(self, path: str, *, timeout: float = 10.0) -> None:
        self.path = os.fspath(path)
        self.timeout = max(0.0, float(timeout))
        self._fd: int | None = None

    def acquire(self) -> "KernelFileLock":
        if self._fd is not None:
            raise RuntimeError("This lock instance is already held")
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        # Windows permits locking a byte beyond EOF.
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fd = fd
                    return self
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise  # unsupported locking fails closed
                    if time.monotonic() >= deadline:
                        raise LockTimeout(f"Timed out waiting for {self.path}") from exc
                    time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "KernelFileLock":
        return self.acquire()

    def __exit__(self, *exc) -> bool:
        self.release()
        return False
