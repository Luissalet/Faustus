"""Small, safe helpers around files and zip archives: names, never-overwrite writes, zip-slip and zip-bomb guards.

Standard library only. (Size-limited zip *splitting* is a service owned by Disk Hoard, not part of the library.)

* :func:`safe_stem`, :func:`safe_filename` — names every OS accepts (delegates to :func:`hoard_link.text.safe_filename`).
* :func:`write_unique` — write ``name``, or ``name (2)``, ``name (3)`` … when taken; created exclusively so two jobs
  racing for the same name cannot overwrite each other.
* :func:`check_zip` — refuse zip bombs (re-exported from :mod:`~hoard_link.docs.readers_lite`).
* :func:`safe_member` — where a zip member may be extracted, refusing ``../`` and absolute paths.

Replaces: Kafka ``workshop/names.py`` (``safe_stem``, ``safe_filename``, ``write_unique``), Cicero ``_check_zip``.
"""

from __future__ import annotations

import os
import posixpath
from pathlib import Path
from typing import Union

from ..text import safe_filename as _safe_filename
from .readers_lite import ZipBombError, check_zip

__all__ = ["safe_stem", "safe_filename", "write_unique", "unique_path", "check_zip", "ZipBombError", "safe_member"]

MAX_STEM = 110


def _basename(name: object) -> str:
    return posixpath.basename(str("" if name is None else name).replace("\\", "/"))


def safe_stem(name: object, default: str = "document", *, max_len: int = MAX_STEM) -> str:
    """A file name part valid on Windows, macOS and Linux: directories dropped, no reserved characters or device
    names, no trailing dots or spaces. The whole ``name`` is kept (``a.b`` stays ``a.b``)."""
    return _safe_filename(_basename(name), max_len=max_len, fallback=default, keep_extension=False)


def safe_filename(name: object, default: str = "file", *, max_len: int = 120) -> str:
    """A whole file name (stem and extension) made safe; directories dropped, the extension kept."""
    return _safe_filename(_basename(name), max_len=max_len, fallback=default)


def _split_name(name: str) -> tuple[str, str]:
    stem, ext = os.path.splitext(name)
    if not stem:                                    # ".hidden"
        return name, ""
    return stem, ext


def unique_path(directory: Union[str, Path], name: str) -> Path:
    """The first free path ``directory/name``, ``directory/name (2)`` … (not reserved: prefer :func:`write_unique`)."""
    d = Path(directory)
    stem, ext = _split_name(safe_filename(name))
    for n in range(1, 10_000):
        p = d / (f"{stem}{ext}" if n == 1 else f"{stem} ({n}){ext}")
        if not p.exists():
            return p
    raise OSError(f"no free name for {name!r} in {d}")


def write_unique(directory: Union[str, Path], name: str, data: bytes) -> Path:
    """Write ``data`` to ``directory/name`` or, when that exists, ``directory/stem (2).ext``, ``(3)`` …; returns the
    path used. The file is created exclusively (nothing that exists is ever overwritten) and a failed write leaves
    no half file. ``name`` is made safe first."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    stem, ext = _split_name(safe_filename(name))
    for n in range(1, 10_000):
        target = d / (f"{stem}{ext}" if n == 1 else f"{stem} ({n}){ext}")
        try:
            handle = open(target, "xb")
        except FileExistsError:
            continue
        try:
            with handle:
                handle.write(data)
        except BaseException:
            try:
                target.unlink()
            except OSError:
                pass
            raise
        return target
    raise OSError(f"no free name for {name!r} in {d}")


def safe_member(base: Union[str, Path], name: str) -> Path:
    """The destination of zip member ``name`` under ``base`` (not created). ``ValueError`` for absolute paths, drive
    letters, ``..`` segments, NUL bytes or anything that would land outside ``base`` (zip-slip). Backslashes are
    path separators; ``./`` and empty segments are ignored."""
    raw = str(name or "")
    if "\x00" in raw:
        raise ValueError(f"unsafe archive member name: {raw!r}")
    unified = raw.replace("\\", "/")
    if unified.startswith("/") or (len(unified) > 1 and unified[1] == ":" and unified[0].isalpha()):
        raise ValueError(f"absolute path in archive: {raw!r}")
    parts = [p for p in unified.split("/") if p not in ("", ".")]
    if not parts:
        raise ValueError("empty archive member name")
    if any(p == ".." for p in parts):
        raise ValueError(f"path traversal in archive: {raw!r}")
    root = Path(base).resolve()
    target = root.joinpath(*parts).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"archive member escapes the destination: {raw!r}") from None
    return target
