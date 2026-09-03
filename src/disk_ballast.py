"""Disk ballast — buy real headroom before the disk runs out (FAUSTUS).

Why this exists
---------------
Shadow-repo checkpoints and Ollama's GGUFs fill a disk fast, and running out of
space *mid-turn* corrupts more than it saves: a half-written SQLite page, a
truncated checkpoint, a settings file replaced by nothing. The cure is boring
and old: keep a few big files around that occupy real blocks, and delete them
the instant the disk gets tight. Unlinking a preallocated gigabyte is
instantaneous and cannot fail for lack of space, so it buys the minutes in
which something — a human, or the scoring below — decides what to remove *for
real*.

Four things this module is careful about
----------------------------------------
1. **The ballast must never fill the disk it protects.** Every allocation is
   refused when the free space it would leave falls under a floor
   (:func:`floor_bytes`), the write is incremental and re-checks free space
   between chunks, a partial file is removed when the check trips, and the
   ballast as a whole may never exceed ``MAX_BALLAST_FRACTION`` of the volume.
2. **Nothing is ever deleted directly.** :func:`quarantine` *moves* a candidate
   into ``DATA_DIR/_quarantine/<id>/`` with a manifest, and :func:`undo`
   puts it back. Only :func:`sweep` — explicit, and only for entries older
   than 24 hours — destroys anything.
3. **The ``.git/`` veto is absolute.** A candidate with a git directory
   anywhere inside it is never deletable, whatever it scores, and a candidate
   this module could not finish scanning is vetoed too: "I did not look"
   resolves to "do not touch", never to "probably fine".
4. **The default mode is ``observe``.** In observe mode this module measures
   and reports and moves nothing at all, so shipping it changes nothing until
   the user opts in. ``canary`` allows at most ``CANARY_PER_HOUR`` quarantines
   an hour; ``enforce`` allows them at the rate the caller asks for. This is
   the same ladder ``src/command_guard.py`` uses; there is no ``off`` because
   observe *is* this module's off — it never writes, moves or deletes.

Urgency
-------
:class:`UrgencyEstimator` is pure maths with an injectable clock: an EWMA of
the free-space rate and of its acceleration, projected over a horizon with
``distance = rate·t + ½·accel·t²``, turned into an error against the floor and
fed through a PID (Kp=0.25, Ki=0.08, Kd=0.02) clamped to 0..1. It reads no
disk and no settings, so it is tested against hand-computed values.

Pure stdlib. Nothing here may raise into a hot path: every public entry point
returns a dict that says what happened, including when what happened was an
OSError.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

try:  # pragma: no cover - constants always import in the app
    from src.constants import DATA_DIR as _DEFAULT_DATA_DIR
except Exception:  # noqa: BLE001 - standalone use (tests, tooling)
    _DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# Module-level so tests can point the ballast and the quarantine somewhere
# disposable, exactly as src/command_guard.py and src/memory_engine.py do.
DATA_DIR = _DEFAULT_DATA_DIR

BALLAST_DIRNAME = "ballast"
QUARANTINE_DIRNAME = "_quarantine"
BALLAST_PREFIX = "ballast-"
BALLAST_SUFFIX = ".bin"

MODES: Tuple[str, ...] = ("observe", "canary", "enforce")
DEFAULT_MODE = "observe"
MODE_SETTING = "agent_disk_ballast"
CANARY_PER_HOUR = 10

# Ballast geometry.
GIB = 1024 * 1024 * 1024
DEFAULT_BALLAST_COUNT = 4
DEFAULT_BALLAST_BYTES = GIB
WRITE_CHUNK_BYTES = 4 * 1024 * 1024
# Re-check the free space every this many chunks while writing.
FREE_CHECK_EVERY_CHUNKS = 8

# The floor the ballast may never eat into, and the share of the volume the
# ballast as a whole may never exceed.
FLOOR_MIN_BYTES = 2 * GIB
FLOOR_FRACTION = 0.05
MAX_BALLAST_FRACTION = 0.25

# Scoring.
AGE_FULL_DAYS = 90.0
SIZE_FULL_BYTES = float(GIB)
W_AGE, W_SIZE, W_REDERIVABLE = 0.45, 0.30, 0.25

# Bounded walks: a candidate we cannot finish scanning is vetoed, not guessed.
MAX_WALK_ENTRIES = 20_000

QUARANTINE_SWEEP_HOURS = 24.0

_GIT_DIRNAME = ".git"
_ROTATED_LOG_RE = re.compile(r"\.(?:log|jsonl|json|txt|out|err)\.(?:\d+|gz|bak|old)$", re.I)

_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: Optional[datetime] = None) -> str:
    return (moment or _utcnow()).isoformat()


def _clamp(value: float, low: float, high: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return low
    if num != num:  # NaN
        return low
    return max(low, min(high, num))


def _clamp01(value: float) -> float:
    return _clamp(value, 0.0, 1.0)


def data_dir() -> str:
    return DATA_DIR


def ballast_dir() -> str:
    return os.path.join(DATA_DIR, BALLAST_DIRNAME)


def quarantine_dir() -> str:
    return os.path.join(DATA_DIR, QUARANTINE_DIRNAME)


def disk_usage(path: Optional[str] = None) -> Tuple[int, int, int]:
    """``(total, used, free)`` of the volume holding ``path``.

    A module-level function on purpose: it is the single place the real disk is
    read, so a test can replace it with a fake free-space source without any
    of the allocation logic knowing.
    """
    target = path or DATA_DIR
    probe = target
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    usage = shutil.disk_usage(probe or os.getcwd())
    return int(usage.total), int(usage.used), int(usage.free)


def floor_bytes(total: Optional[int] = None) -> int:
    """Free bytes the ballast may never eat into."""
    if total is None:
        try:
            total = disk_usage()[0]
        except Exception:  # noqa: BLE001 - a probe failure must not raise
            total = 0
    return int(max(FLOOR_MIN_BYTES, float(total or 0) * FLOOR_FRACTION))


def mode() -> str:
    """Current ballast mode. Unknown or unreadable settings mean ``observe``."""
    try:
        from src.settings import get_setting
        raw = str(get_setting(MODE_SETTING, DEFAULT_MODE) or "").strip().lower()
    except Exception:  # noqa: BLE001 - a settings failure must not raise
        return DEFAULT_MODE
    return raw if raw in MODES else DEFAULT_MODE


# ---------------------------------------------------------------------------
# Urgency — EWMA rate + acceleration, projected, through a PID
# ---------------------------------------------------------------------------

DEFAULT_HORIZON_S = 3600.0
DEFAULT_ALPHA = 0.5
KP, KI, KD = 0.25, 0.08, 0.02


class UrgencyEstimator:
    """How worried should we be, on a 0..1 scale.

    ``observe(free_bytes, now)`` takes one sample of the volume's free space at
    a monotonic-ish wall time; ``urgency()`` reads the current value.

    The maths, in order:

    * ``rate`` — EWMA (weight ``alpha``) of ``Δfree/Δt``; negative while the
      disk fills.
    * ``accel`` — EWMA of ``Δrate/Δt``, so a fill that is *speeding up* is
      worse than a steady one at the same rate.
    * ``distance = rate·H + ½·accel·H²`` — where the free space is projected to
      be ``H`` seconds from now.
    * ``error = max(0, (floor − projected_free) / floor)`` — 0 while the
      projection stays above the floor, 1 when it projects to zero or below.
    * ``urgency = clamp01(Kp·e + Ki·∫e + Kd·de)``, with the integral and the
      derivative taken in units of the horizon (``τ = Δt/H``) so the constants
      do not depend on how often the caller samples.

    The integral takes the *signed* distance from the floor, so calm drains it
    at the rate pressure filled it; wind-up is bounded on both sides (0 and
    1/Ki, so the I term alone can never exceed 1) and the derivative is clamped
    to ±1, so a single tiny ``Δt`` cannot spike the output. Samples that do not
    advance the clock are ignored rather than dividing by zero.

    No disk, no settings, no clock of its own: everything is passed in.
    """

    __slots__ = ("horizon_s", "alpha", "floor", "kp", "ki", "kd",
                 "_free", "_t", "_rate", "_accel", "_error", "_integral",
                 "_urgency", "_samples", "_projected")

    def __init__(self, *, floor: int = FLOOR_MIN_BYTES,
                 horizon_s: float = DEFAULT_HORIZON_S,
                 alpha: float = DEFAULT_ALPHA,
                 kp: float = KP, ki: float = KI, kd: float = KD) -> None:
        self.horizon_s = float(horizon_s) if horizon_s and horizon_s > 0 else DEFAULT_HORIZON_S
        self.alpha = _clamp01(alpha if alpha is not None else DEFAULT_ALPHA)
        self.floor = max(0, int(floor or 0))
        self.kp, self.ki, self.kd = float(kp), float(ki), float(kd)
        self._free: Optional[float] = None
        self._t: Optional[float] = None
        self._rate: Optional[float] = None
        self._accel: float = 0.0
        self._error: float = 0.0
        self._integral: float = 0.0
        self._urgency: float = 0.0
        self._projected: Optional[float] = None
        self._samples: int = 0

    # -- reads ----------------------------------------------------------

    def urgency(self) -> float:
        return self._urgency

    def state(self) -> Dict[str, Any]:
        return {
            "value": round(self._urgency, 6),
            "samples": self._samples,
            "rate_bytes_per_s": None if self._rate is None else round(self._rate, 3),
            "accel_bytes_per_s2": round(self._accel, 6),
            "error": round(self._error, 6),
            "integral": round(self._integral, 6),
            "horizon_s": self.horizon_s,
            "floor_bytes": self.floor,
            "projected_free_bytes": (None if self._projected is None
                                     else int(self._projected)),
        }

    # -- the one write --------------------------------------------------

    def observe(self, free_bytes: Any, now: Any) -> float:
        """Take one sample; return the urgency after it. Never raises."""
        try:
            free = float(free_bytes)
            stamp = float(now)
        except (TypeError, ValueError):
            return self._urgency
        if free != free or stamp != stamp:  # NaN
            return self._urgency

        if self._t is None or self._free is None:
            self._free, self._t, self._samples = free, stamp, 1
            return self._urgency

        dt = stamp - self._t
        if dt <= 0:
            # A clock that went backwards or stood still tells us nothing about
            # a rate, and dividing by it would tell us something false.
            self._free = free
            return self._urgency

        raw_rate = (free - self._free) / dt
        if self._rate is None:
            rate, accel = raw_rate, 0.0
        else:
            rate = self.alpha * raw_rate + (1.0 - self.alpha) * self._rate
            raw_accel = (rate - self._rate) / dt
            accel = self.alpha * raw_accel + (1.0 - self.alpha) * self._accel

        horizon = self.horizon_s
        distance = rate * horizon + 0.5 * accel * horizon * horizon
        projected = free + distance

        if self.floor > 0:
            signed = _clamp((self.floor - projected) / float(self.floor), -1.0, 1.0)
        else:
            signed = -1.0
        error = max(0.0, signed)

        tau = dt / horizon
        # The integral takes the SIGNED error, so a horizon of calm drains what
        # a horizon of pressure filled. With a one-sided error it would wind up
        # permanently and a disk that recovered would stay at urgency 1 for
        # ever, which is the way this kind of signal usually goes wrong.
        integral = self._integral + signed * tau
        if self.ki > 0:
            integral = _clamp(integral, 0.0, 1.0 / self.ki)
        else:
            integral = max(0.0, integral)
        derivative = _clamp((error - self._error) / tau, -1.0, 1.0) if tau > 0 else 0.0

        self._free, self._t = free, stamp
        self._rate, self._accel = rate, accel
        self._error, self._integral = error, integral
        self._projected = projected
        self._samples += 1
        self._urgency = _clamp01(self.kp * error + self.ki * integral + self.kd * derivative)
        return self._urgency


_estimator: Optional[UrgencyEstimator] = None


def estimator() -> UrgencyEstimator:
    """The process-wide estimator, floored against the current volume."""
    global _estimator
    with _lock:
        if _estimator is None:
            _estimator = UrgencyEstimator(floor=floor_bytes())
        return _estimator


def reset_estimator(floor: Optional[int] = None) -> UrgencyEstimator:
    global _estimator
    with _lock:
        _estimator = UrgencyEstimator(floor=floor_bytes() if floor is None else int(floor))
        return _estimator


def observe(free_bytes: Any, now: Any = None) -> float:
    """Sample the process-wide estimator. Never raises."""
    stamp = time.time() if now is None else now
    return estimator().observe(free_bytes, stamp)


def urgency() -> float:
    return estimator().urgency()


# ---------------------------------------------------------------------------
# Ballast files
# ---------------------------------------------------------------------------


def _ballast_files() -> List[str]:
    directory = ballast_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    keep = [
        n for n in names
        if n.startswith(BALLAST_PREFIX) and n.endswith(BALLAST_SUFFIX)
    ]
    return [os.path.join(directory, n) for n in sorted(keep)]


def _file_size(path: str) -> int:
    try:
        return int(os.path.getsize(path))
    except OSError:
        return 0


def ballast_state() -> Dict[str, Any]:
    files = _ballast_files()
    rows = [{"name": os.path.basename(p), "bytes": _file_size(p)} for p in files]
    return {
        "dir": ballast_dir(),
        "count": len(rows),
        "bytes": sum(r["bytes"] for r in rows),
        "files": rows,
    }


def _next_index(existing: Sequence[str]) -> int:
    used = set()
    for path in existing:
        stem = os.path.basename(path)[len(BALLAST_PREFIX):-len(BALLAST_SUFFIX)]
        try:
            used.add(int(stem))
        except ValueError:
            continue
    index = 0
    while index in used:
        index += 1
    return index


class _Aborted(Exception):
    """Internal: the allocation stopped on purpose, and cleans up after itself."""


def _write_one(path: str, size_bytes: int, floor: int,
               should_abort: Optional[Callable[[], bool]]) -> Tuple[bool, str]:
    """Write one ballast file with real blocks. ``(ok, reason)``.

    Incremental and abortable: the free space is re-checked every
    ``FREE_CHECK_EVERY_CHUNKS`` chunks and ``should_abort`` every chunk, and a
    file that could not be finished is removed rather than left as a partial
    occupant of the disk it was supposed to protect.
    """
    chunk = b"\0" * min(WRITE_CHUNK_BYTES, max(1, size_bytes))
    written = 0
    chunks = 0
    try:
        with open(path, "wb") as fh:
            while written < size_bytes:
                if should_abort is not None and should_abort():
                    raise _Aborted("caller asked to stop")
                take = min(len(chunk), size_bytes - written)
                fh.write(chunk[:take] if take != len(chunk) else chunk)
                written += take
                chunks += 1
                if chunks % FREE_CHECK_EVERY_CHUNKS == 0:
                    fh.flush()
                    try:
                        free_now = disk_usage(path)[2]
                    except Exception:  # noqa: BLE001 - probe failure: stop, safely
                        raise _Aborted("free space could not be re-checked")
                    if free_now < floor:
                        raise _Aborted("free space fell below the floor while allocating")
            fh.flush()
            os.fsync(fh.fileno())
        return True, ""
    except _Aborted as stop:
        _unlink(path)
        return False, str(stop)
    except OSError as exc:
        _unlink(path)
        return False, f"write failed: {exc}"


def _unlink(path: str) -> int:
    size = _file_size(path)
    try:
        os.unlink(path)
        return size
    except OSError:
        return 0


def ensure(count: Optional[int] = None, size_bytes: Optional[int] = None, *,
           should_abort: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Bring the ballast up to ``count`` files of ``size_bytes`` each.

    Refuses — with a reason, never an exception — rather than allocating when
    the result would leave less than :func:`floor_bytes` free, or when the
    ballast would exceed ``MAX_BALLAST_FRACTION`` of the volume. Files already
    present are kept: this is idempotent and safe to call on every start.
    """
    target = DEFAULT_BALLAST_COUNT if count is None else int(count)
    size = DEFAULT_BALLAST_BYTES if size_bytes is None else int(size_bytes)
    result: Dict[str, Any] = {"ok": True, "created": [], "created_bytes": 0,
                              "reason": "", "target_count": target,
                              "target_bytes": size}
    if target < 0 or size <= 0:
        result.update(ok=False, reason="count must be >= 0 and size_bytes > 0")
        result["ballast"] = ballast_state()
        return result

    with _lock:
        try:
            os.makedirs(ballast_dir(), exist_ok=True)
        except OSError as exc:
            result.update(ok=False, reason=f"ballast directory unusable: {exc}")
            result["ballast"] = ballast_state()
            return result

        existing = _ballast_files()
        try:
            total, _used, free = disk_usage(ballast_dir())
        except Exception as exc:  # noqa: BLE001
            result.update(ok=False, reason=f"free space unreadable: {exc}")
            result["ballast"] = ballast_state()
            return result
        floor = floor_bytes(total)
        cap = int(total * MAX_BALLAST_FRACTION)
        held = sum(_file_size(p) for p in existing)

        while len(existing) < target:
            if held + size > cap:
                result["reason"] = (
                    f"ballast would exceed {int(MAX_BALLAST_FRACTION * 100)}% of the volume"
                )
                break
            if free - size < floor:
                result["reason"] = (
                    "refused: allocating would leave less than the floor free "
                    f"({free - size} < {floor})"
                )
                break
            path = os.path.join(ballast_dir(),
                                f"{BALLAST_PREFIX}{_next_index(existing):02d}{BALLAST_SUFFIX}")
            ok, why = _write_one(path, size, floor, should_abort)
            if not ok:
                result["reason"] = why
                break
            existing.append(path)
            existing.sort()
            held += size
            free -= size
            result["created"].append(os.path.basename(path))
            result["created_bytes"] += size

        result["ok"] = not result["reason"] or bool(result["created"])
        result["ballast"] = ballast_state()
        result["floor_bytes"] = floor
        return result


def release(n: int = 1) -> Dict[str, Any]:
    """Unlink ``n`` ballast files — the instant headroom. Never raises."""
    want = max(0, int(n or 0))
    freed, removed = 0, []
    with _lock:
        files = _ballast_files()
        # Highest index first, so the remaining set stays 0..k-1.
        for path in list(reversed(files))[:want]:
            size = _unlink(path)
            if size or not os.path.exists(path):
                removed.append(os.path.basename(path))
                freed += size
    return {"ok": True, "released": removed, "freed_bytes": freed,
            "ballast": ballast_state()}


# ---------------------------------------------------------------------------
# Candidates — what Faustus itself made, scored, with its vetoes
# ---------------------------------------------------------------------------

# (subdirectory, granularity, re-derivability, why it is re-derivable)
CANDIDATE_ROOTS: Tuple[Tuple[str, str, float, str], ...] = (
    ("checkpoints", "children", 0.55, "shadow-repo checkpoint of a workspace"),
    ("dispatch", "children", 0.85, "JSON mirror of a finished dispatch job"),
    ("fastembed_cache", "self", 1.00, "embedding model cache, re-downloaded on demand"),
    ("tts_cache", "self", 1.00, "spoken audio, re-synthesised on demand"),
    ("emoji_cache", "self", 1.00, "emoji images, re-fetched on demand"),
    ("email_urgency_cache", "self", 1.00, "recomputed from the mailbox"),
    ("generated_images", "children", 0.15, "an image the user asked for"),
)

KIND_ROTATED_LOG = "rotated_log"
ROTATED_LOG_REDERIVABLE = 0.95


def _walk_stats(path: str) -> Tuple[int, float, bool, bool]:
    """``(size_bytes, newest_mtime, has_git, complete)`` for a file or tree.

    ``complete`` is False when the walk hit ``MAX_WALK_ENTRIES`` or an OSError,
    which the caller must treat as "unknown", never as "clean".
    """
    try:
        if os.path.islink(path):
            st = os.lstat(path)
            return int(st.st_size), float(st.st_mtime), False, True
        if os.path.isfile(path):
            st = os.stat(path)
            return int(st.st_size), float(st.st_mtime), False, True
    except OSError:
        return 0, 0.0, False, False

    size = 0
    newest = 0.0
    seen = 0
    has_git = False
    complete = True
    try:
        newest = float(os.stat(path).st_mtime)
    except OSError:
        return 0, 0.0, False, False
    for root, dirs, files in os.walk(path, followlinks=False):
        if _GIT_DIRNAME in dirs or os.path.basename(root) == _GIT_DIRNAME:
            has_git = True
        for name in files:
            seen += 1
            if seen > MAX_WALK_ENTRIES:
                complete = False
                break
            full = os.path.join(root, name)
            try:
                st = os.lstat(full)
            except OSError:
                complete = False
                continue
            size += int(st.st_size)
            newest = max(newest, float(st.st_mtime))
        for name in dirs:
            try:
                newest = max(newest, float(os.lstat(os.path.join(root, name)).st_mtime))
            except OSError:
                complete = False
        if not complete:
            break
    return size, newest, has_git, complete


def _inside_data_dir(path: str) -> bool:
    try:
        root = os.path.realpath(DATA_DIR)
        target = os.path.realpath(path)
    except OSError:
        return False
    try:
        return os.path.commonpath([root, target]) == root and target != root
    except ValueError:  # different drives on Windows
        return False


def _vetoes(path: str, has_git: bool, complete: bool) -> List[str]:
    """Every reason this candidate may not be touched. Empty = deletable."""
    out: List[str] = []
    if has_git:
        out.append("contains a .git directory — version control is never a cache")
    if not _inside_data_dir(path):
        out.append("outside DATA_DIR")
    if not complete:
        out.append("could not be fully scanned, so a .git directory cannot be ruled out")
    try:
        real = os.path.realpath(path)
        for reserved, why in ((ballast_dir(), "the ballast itself"),
                              (quarantine_dir(), "the quarantine itself")):
            reserved_real = os.path.realpath(reserved)
            if real == reserved_real or real.startswith(reserved_real + os.sep):
                out.append(why)
    except OSError:
        out.append("path could not be resolved")
    return out


def score_candidate(*, size_bytes: int, age_days: float, rederivable: float) -> float:
    """``0.45·age + 0.30·size + 0.25·re-derivability``, each part in 0..1."""
    age_norm = _clamp01(age_days / AGE_FULL_DAYS)
    size_norm = _clamp01(size_bytes / SIZE_FULL_BYTES)
    return round(W_AGE * age_norm + W_SIZE * size_norm
                 + W_REDERIVABLE * _clamp01(rederivable), 6)


def _candidate(path: str, kind: str, rederivable: float, why: str,
               now_ts: float) -> Optional[Dict[str, Any]]:
    size, newest, has_git, complete = _walk_stats(path)
    if newest <= 0 and size == 0 and not os.path.exists(path):
        return None
    age_days = max(0.0, (now_ts - newest) / 86400.0) if newest > 0 else 0.0
    vetoes = _vetoes(path, has_git, complete)
    reasons = [
        f"{why}",
        f"last touched {age_days:.1f} days ago",
        f"{size} bytes",
    ]
    row: Dict[str, Any] = {
        "path": path,
        "name": os.path.basename(path),
        "kind": kind,
        "size_bytes": size,
        "age_days": round(age_days, 3),
        "rederivable": round(_clamp01(rederivable), 3),
        "reasons": reasons,
        "vetoes": vetoes,
        "deletable": not vetoes,
        "scan_complete": complete,
    }
    row["score"] = 0.0 if vetoes else score_candidate(
        size_bytes=size, age_days=age_days, rederivable=rederivable)
    return row


def scan(*, now: Optional[float] = None, limit: int = 200) -> List[Dict[str, Any]]:
    """Score everything Faustus itself created that could go, worst first.

    Vetoed candidates are listed too, with score 0 and their vetoes spelled
    out: the point of this list is that a human can see what was *not*
    considered and why.
    """
    now_ts = time.time() if now is None else float(now)
    rows: List[Dict[str, Any]] = []
    try:
        for sub, granularity, rederivable, why in CANDIDATE_ROOTS:
            root = os.path.join(DATA_DIR, sub)
            if not os.path.exists(root):
                continue
            if granularity == "self":
                row = _candidate(root, sub, rederivable, why, now_ts)
                if row:
                    rows.append(row)
                continue
            try:
                children = sorted(os.listdir(root))
            except OSError:
                continue
            for name in children:
                row = _candidate(os.path.join(root, name), sub, rederivable, why, now_ts)
                if row:
                    rows.append(row)
        # Rotated logs sitting directly under DATA_DIR.
        try:
            for name in sorted(os.listdir(DATA_DIR)):
                full = os.path.join(DATA_DIR, name)
                if os.path.isfile(full) and _ROTATED_LOG_RE.search(name):
                    row = _candidate(full, KIND_ROTATED_LOG, ROTATED_LOG_REDERIVABLE,
                                     "a rotated log file", now_ts)
                    if row:
                        rows.append(row)
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001 - a scan failure reports, never raises
        logger.debug("disk ballast: scan failed: %s", exc)
    rows.sort(key=lambda r: (-r["score"], r["path"]))
    return rows[:max(1, int(limit or 200))]


# ---------------------------------------------------------------------------
# Quarantine — the only way anything leaves its place, and it comes back
# ---------------------------------------------------------------------------


def _entries_dir() -> str:
    return quarantine_dir()


def _read_entry(entry_dir: str) -> Optional[Dict[str, Any]]:
    try:
        with open(os.path.join(entry_dir, "entry.json"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("id"):
            data["entry_dir"] = entry_dir
            return data
    except (OSError, ValueError):
        return None
    return None


def list_quarantine() -> List[Dict[str, Any]]:
    """Every quarantined entry, newest first. Never raises."""
    out: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(_entries_dir()))
    except OSError:
        return out
    for name in names:
        entry = _read_entry(os.path.join(_entries_dir(), name))
        if entry:
            out.append(entry)
    out.sort(key=lambda e: str(e.get("quarantined_at") or ""), reverse=True)
    return out


def _recent_quarantines(now: datetime, hours: float = 1.0) -> int:
    count = 0
    for entry in list_quarantine():
        try:
            stamp = datetime.fromisoformat(str(entry.get("quarantined_at")))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if (now - stamp).total_seconds() <= hours * 3600.0:
            count += 1
    return count


def quarantine(path: Any, *, reason: str = "", now: Optional[datetime] = None,
               mode_override: Optional[str] = None) -> Dict[str, Any]:
    """Move ``path`` out of the way — never delete it. ``{"ok", ...}``.

    Refused, with the reason in the answer, when: the mode is ``observe``
    (which moves nothing, ever), the canary budget for this hour is spent, the
    path is outside ``DATA_DIR``, or any veto applies — the ``.git`` veto
    included, which no mode and no caller can override.
    """
    target = str(path or "")
    stamp = now or _utcnow()
    active = (mode_override or mode()).strip().lower()
    if active not in MODES:
        active = DEFAULT_MODE
    answer: Dict[str, Any] = {"ok": False, "mode": active, "path": target,
                              "id": None, "reason": ""}
    if not target:
        answer["reason"] = "no path given"
        return answer
    if active == "observe":
        answer["reason"] = ("observe mode: the candidate was scored and reported, "
                            "nothing was moved")
        return answer

    with _lock:
        try:
            if not os.path.exists(target):
                answer["reason"] = "no such path"
                return answer
            size, _newest, has_git, complete = _walk_stats(target)
            vetoes = _vetoes(target, has_git, complete)
            if vetoes:
                answer["reason"] = "vetoed: " + "; ".join(vetoes)
                answer["vetoes"] = vetoes
                return answer
            if active == "canary":
                recent = _recent_quarantines(stamp)
                if recent >= CANARY_PER_HOUR:
                    answer["reason"] = (f"canary mode: {recent} quarantines in the last "
                                        f"hour, the budget is {CANARY_PER_HOUR}")
                    return answer

            entry_id = f"{stamp.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
            entry_dir = os.path.join(_entries_dir(), entry_id)
            os.makedirs(entry_dir, exist_ok=False)
            payload = os.path.join(entry_dir, os.path.basename(target.rstrip(os.sep)) or "payload")
            shutil.move(target, payload)
            record = {
                "id": entry_id,
                "original_path": os.path.abspath(target),
                "payload": payload,
                "size_bytes": size,
                "reason": str(reason or "")[:400],
                "mode": active,
                "quarantined_at": _iso(stamp),
                "sweep_after_hours": QUARANTINE_SWEEP_HOURS,
            }
            with open(os.path.join(entry_dir, "entry.json"), "w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, indent=2)
            answer.update(ok=True, id=entry_id, entry=record,
                          reason="moved to quarantine; undo restores it")
            return answer
        except Exception as exc:  # noqa: BLE001 - never raises into a caller
            logger.warning("disk ballast: quarantine of %s failed: %r", target, exc)
            answer["reason"] = f"quarantine failed: {exc}"
            return answer


def undo(entry_id: Any) -> Dict[str, Any]:
    """Put a quarantined entry back where it came from. Never raises."""
    wanted = str(entry_id or "")
    answer: Dict[str, Any] = {"ok": False, "id": wanted, "reason": ""}
    if not wanted:
        answer["reason"] = "no id given"
        return answer
    with _lock:
        entry_dir = os.path.join(_entries_dir(), wanted)
        entry = _read_entry(entry_dir)
        if not entry:
            answer["reason"] = "no such quarantine entry"
            return answer
        original = str(entry.get("original_path") or "")
        payload = str(entry.get("payload") or "")
        try:
            if not original or not os.path.exists(payload):
                answer["reason"] = "the quarantined payload is gone"
                return answer
            if os.path.exists(original):
                answer["reason"] = ("something is back at the original path; "
                                    "refusing to overwrite it")
                answer["payload"] = payload
                return answer
            os.makedirs(os.path.dirname(original) or ".", exist_ok=True)
            shutil.move(payload, original)
            shutil.rmtree(entry_dir, ignore_errors=True)
            answer.update(ok=True, restored_to=original, reason="restored")
            return answer
        except Exception as exc:  # noqa: BLE001
            logger.warning("disk ballast: undo of %s failed: %r", wanted, exc)
            answer["reason"] = f"undo failed: {exc}"
            return answer


def sweep(*, now: Optional[datetime] = None,
          max_age_hours: float = QUARANTINE_SWEEP_HOURS) -> Dict[str, Any]:
    """Destroy quarantine entries older than ``max_age_hours``.

    This is the ONLY function in this module that removes user data, it only
    ever touches what is already inside ``DATA_DIR/_quarantine/``, and an
    entry younger than the window is never touched — the undo window is the
    whole point of quarantining rather than deleting.
    """
    stamp = now or _utcnow()
    swept: List[str] = []
    freed = 0
    kept = 0
    with _lock:
        for entry in list_quarantine():
            try:
                when = datetime.fromisoformat(str(entry.get("quarantined_at")))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                kept += 1
                continue
            age_hours = (stamp - when).total_seconds() / 3600.0
            if age_hours < max_age_hours:
                kept += 1
                continue
            entry_dir = str(entry.get("entry_dir") or "")
            if not entry_dir or not _inside_data_dir(entry_dir):
                kept += 1
                continue
            freed += int(entry.get("size_bytes") or 0)
            shutil.rmtree(entry_dir, ignore_errors=True)
            swept.append(str(entry.get("id")))
    return {"ok": True, "swept": swept, "freed_bytes": freed, "kept": kept,
            "max_age_hours": max_age_hours}


# ---------------------------------------------------------------------------
# The one read the route and the UI use
# ---------------------------------------------------------------------------


def status(*, now: Optional[float] = None, limit: int = 200) -> Dict[str, Any]:
    """Free space, urgency, ballast, quarantine and the scored candidates.

    Pure measurement: calling this never allocates, moves or deletes anything,
    in any mode. Never raises — a probe that fails reports zeros and says so.
    """
    now_ts = time.time() if now is None else float(now)
    problems: List[str] = []
    try:
        total, used, free = disk_usage()
    except Exception as exc:  # noqa: BLE001
        total = used = free = 0
        problems.append(f"free space unreadable: {exc}")
    floor = floor_bytes(total)
    est = estimator()
    est.floor = floor
    est.observe(free, now_ts)
    quarantined = list_quarantine()
    return {
        "mode": mode(),
        "modes": list(MODES),
        "data_dir": DATA_DIR,
        "disk": {
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "free_ratio": round(free / total, 6) if total else 0.0,
            "floor_bytes": floor,
            "below_floor": bool(total and free < floor),
        },
        "urgency": est.state(),
        "ballast": ballast_state(),
        "candidates": scan(now=now_ts, limit=limit),
        "quarantine": {
            "dir": quarantine_dir(),
            "count": len(quarantined),
            "bytes": sum(int(e.get("size_bytes") or 0) for e in quarantined),
            "sweep_after_hours": QUARANTINE_SWEEP_HOURS,
            "entries": quarantined,
        },
        "problems": problems,
    }
