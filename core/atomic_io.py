"""Atomic JSON file writes.

Use this everywhere a JSON config file is persisted. A plain `open("w") +
json.dump` truncates the file on first write and only fills it with new
content afterwards — a kill -9 / power loss / OOM in between produces a
truncated or empty file. For password DBs (`auth.json`) and live state
(`sessions.json`, `settings.json`, `integrations.json`, `cookbook_state.json`),
that's a data-loss event.

`atomic_write_json` writes to a sibling tmp file, fsyncs, then `os.replace`s
into place. On POSIX `os.replace` is atomic on the same filesystem.

SEC-1 (B-020): pass ``private=True`` for anything that is or contains a
credential. The temp file is then created with `0600` from its first byte —
not chmod'ed afterwards, which leaves a window where the umask decides who
could read it — and on Windows it gets an explicit owner-only DACL. The
permissions travel with the rename, so the destination inherits them.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Optional

from core.platform_compat import restrict_to_owner


def _open_new(path: str, private: bool, newline: Optional[str] = None):
    """Create `path` exclusively; owner-only from the start when private."""
    if not private:
        return open(path, "w", encoding="utf-8", newline=newline)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8", newline=newline)


def _finish(tmp: str, path: str, private: bool) -> None:
    if private:
        # POSIX already has 0600 from os.open; this is what locks the file
        # down on Windows, where the mode argument is ignored.
        restrict_to_owner(tmp)
    # Windows readers/antivirus can briefly keep the destination open without
    # delete sharing. Retry only the atomic replacement, never truncate/delete
    # the previous file, loosen its ACL, or recreate the already-fsynced bytes.
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            if getattr(exc, 'winerror', None) not in {5, 32, 33} or attempt == 5:
                raise
            time.sleep(0.005 * (2 ** attempt))


def atomic_write_json(
    path: str,
    data: Any,
    *,
    indent: Optional[int] = None,
    private: bool = False,
) -> None:
    """Atomically persist `data` as JSON at `path`.

    The temp file uses a random suffix so two concurrent writers saving the
    same file don't collide on the rename target. A PID suffix does not do
    this: the PID is constant for the life of a process, so two writers on
    the same path within one process (or one single-process container, where
    the PID never changes at all) still race for the same temp file.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{uuid.uuid4().hex}"

    try:
        with _open_new(tmp, private) as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        _finish(tmp, path, private)
    finally:
        # Directly unlink to avoid a check-then-act race condition.
        # Swallows FileNotFoundError (on success path) and other cleanup OSErrors.
        try:
            os.unlink(tmp)
        except OSError:
            pass


def atomic_write_text(path: str, text: str, *, private: bool = False, newline: Optional[str] = None) -> None:
    if not isinstance(text, str):
        raise TypeError("atomic_write_text expects a string")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{uuid.uuid4().hex}"

    try:
        with _open_new(tmp, private, newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _finish(tmp, path, private)
    finally:
        # Directly unlink to avoid a check-then-act race condition.
        # Swallows FileNotFoundError (on success path) and other cleanup OSErrors.
        try:
            os.unlink(tmp)
        except OSError:
            pass
