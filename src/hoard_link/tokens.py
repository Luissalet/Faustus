"""The per-app MCP token and the ``data/url`` file the bridge reads.

Standard library only. The Node twin is ``readOrCreateToken`` / ``checkBearer`` in ``js/hoard-commons/server.js``.

The token is **stable across restarts**. Seven apps used to regenerate and overwrite ``mcp-token`` on every start,
so a second instance (autostart, a double click) changed the file while the running one kept the old token in
memory and the bridge started answering ``401 Invalid MCP token``. :func:`read_or_create_token` only ever creates
a token when the file is missing, shorter than ``min_len`` or unreadable, and creates it atomically (when two
processes race, the one that lost reads the winner's token).
"""

from __future__ import annotations

import hmac
import math
import os
import secrets
from pathlib import Path
from typing import Optional, Union

from .atomic import tmp_path_for, write_bytes_atomic, write_text_atomic

__all__ = ["read_or_create_token", "read_token", "write_url", "read_url", "check_bearer", "new_token"]

PathLike = Union[str, "os.PathLike[str]"]


def new_token(min_len: int = 32) -> str:
    """A URL-safe random token of at least ``min_len`` characters (43 for the default)."""
    return secrets.token_urlsafe(max(32, math.ceil(min_len * 3 / 4) + 1))


def read_token(path: PathLike, *, min_len: int = 32) -> Optional[str]:
    """The stored token, or ``None`` when the file is missing, unreadable or shorter than ``min_len``."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            value = fh.read().strip()
    except (OSError, UnicodeError):
        return None
    return value if len(value) >= min_len and not any(c.isspace() for c in value) else None


def read_or_create_token(path: PathLike, *, min_len: int = 32) -> str:
    """The token stored at ``path``; a new one (written atomically, mode 0600 where supported) when the file is
    missing, unreadable or shorter than ``min_len``. Calling it again, from this or another process, returns the
    same value."""
    existing = read_token(path, min_len=min_len)
    if existing:
        return existing
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    token = new_token(min_len)
    if target.exists():
        again = read_token(path, min_len=min_len)       # somebody created it between our read and now
        if again:
            return again
    else:
        # create-if-absent, atomically and with the full content: link the finished temp file in. Whoever links
        # first wins; the loser adopts the winner's token instead of overwriting it.
        tmp = tmp_path_for(target)
        try:
            write_bytes_atomic(tmp, token.encode("ascii"), mode=0o600)
            try:
                os.link(tmp, target)
                return token
            except FileExistsError:
                again = read_token(path, min_len=min_len)
                if again:
                    return again
            except OSError:
                pass  # no hard links here (FAT, some network shares): fall back to the plain replace below
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    write_bytes_atomic(target, token.encode("ascii"), mode=0o600)
    return token


def write_url(path: PathLike, url: str) -> None:
    """Record the URL this instance listens on (``data/url``), atomically."""
    write_text_atomic(path, str(url).strip(), fsync=False)


def read_url(path: PathLike) -> Optional[str]:
    """The recorded URL, or ``None`` when the file is missing or empty."""
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            value = fh.read().strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def check_bearer(header: Optional[str], token: Optional[str]) -> bool:
    """True when ``header`` is ``Bearer <token>`` (scheme case-insensitive, constant-time comparison). An empty
    token never matches, whatever the header says."""
    if not token or not header:
        return False
    scheme, _, rest = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return False
    candidate = rest.strip()
    if not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), token.encode("utf-8"))
