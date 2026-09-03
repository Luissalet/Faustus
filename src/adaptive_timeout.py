"""adaptive_timeout.py — timeouts learned from what this box actually does.

A fixed idle timeout is wrong in both directions: 300 s kills a legitimate
build that prints nothing for six minutes, and it waits five minutes on a box
where every command finishes in two seconds. The fix is to watch how long the
recent cycles of one KIND of work took and scale the bound to that:

    idle_timeout(key, default) = 3 x median(recent durations for `key`)
                                 clamped to [30, 600] s,
                                 `default` while there are fewer than 3 samples.

In-memory only (a bounded deque of the last 20 durations per key), thread-safe,
and total: recording a bad value or asking for an unknown key never raises and
never changes a timeout. Callers gate on `enabled()` (setting
``agent_adaptive_idle_timeout``); with it off nobody consults this module and
the fixed values stand exactly as they were.

Keys are the caller's choice of "kind of work": a tool name ("bash", "python")
or a worker kind ("dispatch:<workspace>").
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Durations kept per key.
MAX_SAMPLES = 20
#: Below this many samples the median means nothing — the default is used.
MIN_SAMPLES = 3
#: The multiplier applied to the median cycle time.
FACTOR = 3.0
#: The window an adaptive timeout may land in.
MIN_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 600.0

_lock = threading.Lock()
_recent: Dict[str, Deque[float]] = {}


def enabled() -> bool:
    """Setting ``agent_adaptive_idle_timeout``. Off = the fixed values."""
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_adaptive_idle_timeout", True))
    except Exception:  # noqa: BLE001 - never raise into a hot path
        return True


def record(key: str, seconds: float) -> None:
    """Remember how long one cycle of `key` took. Ignores anything that is not
    a usable positive duration."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return
    if value != value or value <= 0 or value == float("inf"):
        return
    name = str(key or "").strip()
    if not name:
        return
    with _lock:
        bucket = _recent.get(name)
        if bucket is None:
            bucket = _recent[name] = deque(maxlen=MAX_SAMPLES)
        bucket.append(value)


def samples(key: str) -> List[float]:
    """The durations remembered for `key`, oldest first."""
    with _lock:
        return list(_recent.get(str(key or "").strip()) or ())


def median(key: str) -> Optional[float]:
    """Median of the remembered durations, or None when there are none."""
    values = sorted(samples(key))
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


def idle_timeout(key: str, default: float, *, factor: float = FACTOR,
                 lo: float = MIN_TIMEOUT_S, hi: float = MAX_TIMEOUT_S) -> float:
    """`factor` x the median recent duration of `key`, clamped to [lo, hi].

    Falls back to `default` with fewer than MIN_SAMPLES samples — an unproven
    key never moves a timeout. `lo`/`hi` let a caller keep the value inside its
    own contract (dispatch passes its fixed estimate as `lo`, so the ceiling it
    reports can only grow).
    """
    try:
        fallback = float(default)
    except (TypeError, ValueError):
        fallback = 0.0
    values = samples(key)
    if len(values) < MIN_SAMPLES:
        return fallback
    mid = median(key)
    if not mid or mid <= 0:
        return fallback
    value = float(factor) * float(mid)
    if value < lo:
        value = float(lo)
    if value > hi:
        value = float(hi)
    return value


def note_difference(key: str, adaptive: float, default: float, *, what: str = "idle timeout") -> None:
    """Log an adaptive value that differs from the fixed one (debug level: this
    is called from tool paths that run constantly)."""
    try:
        if abs(float(adaptive) - float(default)) < 0.5:
            return
        logger.debug("[adaptive] %s for %s: %.0fs instead of the fixed %.0fs (median of %d samples)",
                     what, key, float(adaptive), float(default), len(samples(key)))
    except Exception:  # noqa: BLE001 - logging must never break a tool call
        pass


def reset(key: Optional[str] = None) -> None:
    """Forget everything (tests), or one key's samples."""
    with _lock:
        if key is None:
            _recent.clear()
        else:
            _recent.pop(str(key or "").strip(), None)
