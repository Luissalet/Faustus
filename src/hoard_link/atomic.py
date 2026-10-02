"""Atomic file writes that survive Windows: write to a sibling temp file, flush, replace, retry on sharing violations.

Standard library only. The Node twin is ``js/hoard-commons/server.js`` (``writeJsonAtomic`` / ``readJson``).

Why it exists: ~35 places in the apps called ``os.replace`` bare. On Windows the replace fails with
``PermissionError`` / ``WinError 5, 32, 33`` for as long as *any* other handle has the destination open (a UI poll
reading ``state.json``, the search indexer, an antivirus scan, OneDrive). Those holds last milliseconds, so
:func:`replace_with_retry` backs off for about ten seconds before giving up and removes the temp file when it does.
Without ``fsync`` a power cut can leave the renamed file empty, so the writers flush to disk by default.

* :func:`replace_with_retry`, :func:`write_bytes_atomic`, :func:`write_text_atomic`, :func:`write_json_atomic`.
* :func:`read_json` never raises for a missing, empty or corrupt file (it returns ``default``).
* :func:`update_json` is a read-modify-write under one lock per file inside the process.

Text is written as bytes, so ``\\n`` stays ``\\n`` on Windows (``Path.write_text`` would turn it into ``\\r\\n``).
Temp files are named ``<name>.<pid>.<thread>.<random>.tmp`` in the destination folder (same volume, so the
replace is atomic) and parent folders are created.
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import secrets
import threading
import time
from pathlib import Path, PurePath
from typing import Any, Callable, Optional, Union

__all__ = [
    "replace_with_retry", "write_bytes_atomic", "write_text_atomic", "write_json_atomic", "read_json", "update_json",
    "tmp_path_for",
]

PathLike = Union[str, "os.PathLike[str]"]

#: Windows error codes that mean "somebody else holds the file for a moment": access denied, sharing and lock violations.
RETRYABLE_WINERRORS = (5, 32, 33)


def tmp_path_for(path: PathLike) -> Path:
    """A fresh temp name next to ``path``: ``{name}.{pid}.{thread_id}.{rand}.tmp``."""
    target = Path(path)
    return target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp")


def _unlink_quiet(path: PathLike) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def replace_with_retry(src: PathLike, dst: PathLike, *, attempts: int = 40, delay: float = 0.05,
                       replace: Callable[[Any, Any], Any] = os.replace, sleep: Callable[[float], Any] = time.sleep) -> None:
    """``os.replace`` that survives Windows sharing violations.

    Retries ``PermissionError`` and ``OSError`` with ``winerror`` 5/32/33, sleeping ``min(0.25, delay * (1 + i))``
    between attempts (about ten seconds in total with the defaults). Any other ``OSError`` is raised at once. When
    every attempt fails the temp file ``src`` is removed and the last error is raised. ``replace`` and ``sleep``
    are injectable for tests.
    """
    last: Optional[OSError] = None
    for i in range(max(1, int(attempts))):
        try:
            replace(src, dst)
            return
        except PermissionError as exc:  # WinError 5 / 32 map to PermissionError
            last = exc
        except OSError as exc:
            if getattr(exc, "winerror", None) not in RETRYABLE_WINERRORS:
                raise
            last = exc
        if i + 1 < attempts:
            sleep(min(0.25, delay * (1 + i)))
    _unlink_quiet(src)
    assert last is not None
    raise last


def _fsync_dir(folder: Path) -> None:
    """Flush the directory entry too (POSIX only; Windows cannot open a directory like this)."""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(folder), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_bytes_atomic(path: PathLike, data: bytes, *, fsync: bool = True, mode: Optional[int] = None) -> None:
    """Write ``data`` to a sibling temp file and replace ``path`` with it (retrying on Windows locks).

    ``fsync=True`` (default) flushes the file (and, where supported, its folder) before and after the rename so a
    power cut cannot leave an empty file. ``mode`` (for example ``0o600``) is applied to the temp file before the
    replace where the platform supports it. The temp file is removed if anything fails.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(target)
    try:
        # 0o666 lets the umask decide, like open(); an explicit mode is also applied with chmod below
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666 if mode is None else mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        # os.replace / time.sleep are looked up here, at call time, so tests (and callers) can patch them
        replace_with_retry(tmp, target, replace=os.replace, sleep=time.sleep)
    except BaseException:
        _unlink_quiet(tmp)
        raise
    if fsync:
        _fsync_dir(target.parent)


def write_text_atomic(path: PathLike, text: str, *, encoding: str = "utf-8", fsync: bool = True) -> None:
    """:func:`write_bytes_atomic` of ``text.encode(encoding)`` (no newline translation)."""
    write_bytes_atomic(path, text.encode(encoding), fsync=fsync)


def _json_default(value: Any) -> Any:
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        try:
            return sorted(value)
        except TypeError:
            return list(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if hasattr(value, "__dataclass_fields__"):
        import dataclasses
        return dataclasses.asdict(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def write_json_atomic(path: PathLike, obj: Any, *, indent: Optional[int] = 2, ensure_ascii: bool = False,
                      sort_keys: bool = False, fsync: bool = True) -> None:
    """Serialise ``obj`` (``Path``, ``datetime``, ``set`` and dataclasses are accepted) and write it atomically.
    The document ends with a newline. Serialisation happens *before* anything touches the disk, so an object that
    cannot be serialised leaves the existing file alone."""
    text = json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys, default=_json_default) + "\n"
    write_text_atomic(path, text, fsync=fsync)


def read_json(path: PathLike, default: Any = None, *, encoding: str = "utf-8-sig") -> Any:
    """The parsed JSON file, or ``default`` when it is missing, unreadable, empty or corrupt. Tolerates a BOM
    (``utf-8-sig``). Never raises for those cases."""
    try:
        with open(path, "r", encoding=encoding) as fh:
            raw = fh.read()
    except (OSError, UnicodeError):
        return default
    if not raw.strip():
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: PathLike) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


def update_json(path: PathLike, fn: Callable[[Any], Any], default: Any = None, *, indent: Optional[int] = 2,
                ensure_ascii: bool = False, sort_keys: bool = False, fsync: bool = True) -> Any:
    """Read-modify-write ``path`` under one in-process lock per file.

    ``fn`` receives the current document (a fresh deep copy of ``default`` when the file is missing or corrupt).
    If it returns something other than ``None`` that value is written, otherwise the (mutated) document is. If
    ``fn`` raises, nothing is written. Returns what was written. The lock is per process: two processes updating
    the same file still need their own coordination.
    """
    with _lock_for(path):
        current = read_json(path, None)
        if current is None:
            current = copy.deepcopy(default)
        result = fn(current)
        new = current if result is None else result
        write_json_atomic(path, new, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys, fsync=fsync)
        return new
