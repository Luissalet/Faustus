"""Host paths -> container bind-mount arguments, spelled so the same code works
on POSIX hosts and on Windows hosts running Docker Desktop.

Two details this module exists to get right in one place:

* ``-v SRC:DST`` is ambiguous when SRC starts with a drive letter (``C:\\work``
  contains the ``:`` that separates the two halves). ``--mount
  type=bind,source=...,target=...`` has no such ambiguity, so every mount here
  uses that form and the drive letter is written with forward slashes
  (``C:/work``), which Docker Desktop accepts.
* a path that cannot be written safely inside a comma-separated ``--mount``
  value (comma or double quote), or that is a network share, is refused with a
  reason instead of being guessed at. Mounting the wrong directory is worse
  than not starting.

The functions are pure (no Docker call, no filesystem access) so the Windows
spellings can be asserted on any host.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

_DRIVE = re.compile(r"^(?P<drive>[A-Za-z]):(?P<rest>[\\/].*|)$")
_EXTENDED = re.compile(r"^\\\\\?\\(?!UNC\\)")
_MSYS = re.compile(r"^/(?P<drive>[A-Za-z])(?P<rest>/.*|)$")


class MountError(ValueError):
    """The path cannot be mounted without guessing."""


def looks_like_windows_path(path: str) -> bool:
    text = str(path or "")
    return bool(_DRIVE.match(text) or _EXTENDED.match(text) or text.startswith("\\\\"))


def normalize_host_path(path: str) -> str:
    """The host path in the spelling Docker takes for ``source=``.

    ``C:\\work\\proj`` -> ``C:/work/proj``; ``\\\\?\\C:\\work`` -> ``C:/work``.
    A POSIX path is made absolute and returned unchanged. A UNC share
    (``\\\\server\\share``) is refused: Docker Desktop does not bind-mount
    network shares reliably and the failure mode is an empty directory.
    """
    text = str(path or "").strip()
    if not text:
        raise MountError("empty path")
    if text.startswith("\\\\") and not _EXTENDED.match(text):
        raise MountError(f"network share paths cannot be bind-mounted: {text!r}")
    text = _EXTENDED.sub("", text)
    match = _DRIVE.match(text)
    if match:
        rest = match.group("rest").replace("\\", "/").rstrip("/")
        return f"{match.group('drive').upper()}:{rest or '/'}"
    return os.path.abspath(text)


def to_msys_path(path: str) -> str:
    """``C:\\work`` -> ``//c/work``, the spelling older tooling wants in ``-v``."""
    normalized = normalize_host_path(path)
    match = _DRIVE.match(normalized)
    if not match:
        return normalized
    return f"//{match.group('drive').lower()}{match.group('rest').replace(chr(92), '/')}"


def from_msys_path(path: str) -> str:
    """Inverse of :func:`to_msys_path` for ``/c/work`` or ``//c/work``."""
    text = str(path or "")
    match = _MSYS.match(text[1:] if text.startswith("//") else text)
    if not match:
        return text
    return f"{match.group('drive').upper()}:{match.group('rest') or '/'}"


def bind_mount_args(source: str, target: str, *, readonly: bool = False) -> List[str]:
    """``["--mount", "type=bind,source=...,target=...[,readonly]"]``.

    Raises :class:`MountError` when the path cannot be expressed safely.
    """
    src = normalize_host_path(source)
    if any(ch in src or ch in target for ch in (",", '"', "\n", "\r", "\x00")):
        raise MountError(f"path contains a character that cannot be used in a bind mount: {source!r}")
    if not target.startswith("/"):
        raise MountError(f"container target must be absolute: {target!r}")
    spec = f"type=bind,source={src},target={target}"
    if readonly:
        spec += ",readonly"
    return ["--mount", spec]


def host_user_spec(default: Tuple[int, int] = (1000, 1000)) -> str:
    """``uid:gid`` for ``--user``.

    A non-root POSIX host user keeps its own ids so files it owns stay
    readable/writable inside the mount; everything else (root, Windows) uses
    the fixed unprivileged default.
    """
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return f"{default[0]}:{default[1]}"
    uid, gid = getuid(), getgid()
    if uid == 0:
        return f"{default[0]}:{default[1]}"
    return f"{uid}:{gid}"


def describe_mount(source: str, target: str, readonly: bool) -> dict:
    return {"source_kind": "windows" if looks_like_windows_path(source) else "posix",
            "target": target, "readonly": bool(readonly)}


def safe_normalize(path: Optional[str]) -> Optional[str]:
    try:
        return normalize_host_path(path or "")
    except MountError:
        return None
