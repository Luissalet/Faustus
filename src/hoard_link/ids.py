"""Identifiers: ULIDs that stay ordered even when the clock does not move, prefixed ids and short random ids.

Standard library only. The Node twin is ``newUlid`` / ``newId`` in ``js/hoard-commons/server.js``.

A ULID is 26 Crockford base32 characters: 10 of millisecond timestamp and 16 of randomness. :func:`new_ulid` is
**monotonic within the process**: two ids made in the same millisecond (Windows clocks tick every ~15 ms, so that
is the normal case) still sort in creation order, and so does an id made after the clock stepped backwards. The
older per-app formats (Kafka, Tantalus and Pygmalion put a time prefix in front of random characters) were neither
unique nor ordered inside one tick. Existing ids are never rewritten: validators that match old formats should
accept both.
"""

from __future__ import annotations

import math
import os
import re
import secrets
import threading
import time
from typing import Optional

__all__ = ["new_ulid", "new_id", "short_id", "id_time", "is_ulid", "CROCKFORD"]

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(CROCKFORD)}
_DECODE.update({c.lower(): i for i, c in enumerate(CROCKFORD)})
_DECODE.update({"I": 1, "i": 1, "L": 1, "l": 1, "O": 0, "o": 0})   # Crockford's forgiving reading
_RAND_BITS = 80
_RAND_MAX = (1 << _RAND_BITS) - 1
_TIME_MAX = (1 << 48) - 1


def _encode(number: int, length: int) -> str:
    chars = []
    for _ in range(length):
        number, rem = divmod(number, 32)
        chars.append(CROCKFORD[rem])
    return "".join(reversed(chars))


_lock = threading.Lock()
_last_ms = -1
_last_rand = 0


def _random80() -> int:
    return int.from_bytes(os.urandom(10), "big")


def new_ulid(now: Optional[float] = None) -> str:
    """A 26-character ULID (uppercase Crockford).

    Without ``now`` it uses the wall clock and is strictly increasing within this process (a lock guards the last
    value; a clock that does not advance, or goes back, makes the random part count up instead). With an explicit
    ``now`` (seconds since the epoch; for imports and tests) the time is exactly that and the process state is not
    touched, so such ids are random inside the millisecond.
    """
    global _last_ms, _last_rand
    if now is not None:
        ms = max(0, min(_TIME_MAX, int(now * 1000)))
        return _encode(ms, 10) + _encode(_random80(), 16)
    with _lock:
        ms = int(time.time() * 1000)
        if ms <= _last_ms:
            ms = _last_ms
            rand = _last_rand + 1
            if rand > _RAND_MAX:            # 2**80 ids in one millisecond: move on to the next one
                ms += 1
                rand = _random80() >> 1
        else:
            rand = _random80() >> 1          # headroom so counting up never overflows
        _last_ms, _last_rand = ms, rand
    return _encode(ms, 10) + _encode(rand, 16)


def new_id(prefix: str = "", *, sep: str = "_") -> str:
    """``<prefix><sep><ULID>`` (just the ULID when ``prefix`` is empty): ordered by creation time, safe in file
    names and URLs."""
    ulid = new_ulid()
    return f"{prefix}{sep}{ulid}" if prefix else ulid


def short_id(n: int = 8) -> str:
    """``n`` random lowercase hex characters, for labels that need no ordering."""
    n = max(1, int(n))
    return secrets.token_hex(math.ceil(n / 2))[:n]


_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{25}$")


def is_ulid(value: object) -> bool:
    """True for a bare 26-character ULID (case-insensitive; the first character is at most ``7`` because the
    time is 48 bits)."""
    return isinstance(value, str) and bool(_ULID_RE.match(value))


def id_time(value: object) -> Optional[float]:
    """The creation time (seconds since the epoch) encoded in a ULID or in ``<prefix><sep><ULID>``; ``None`` for
    anything else (uuids, the old per-app formats, a prefix glued to the ULID without a separator)."""
    if not isinstance(value, str) or len(value) < 26:
        return None
    tail = value[-26:]
    if len(value) > 26 and value[-27].isalnum():
        return None
    if not is_ulid(tail):
        return None
    ms = 0
    for ch in tail[:10]:
        ms = ms * 32 + _DECODE[ch]
    return ms / 1000.0
