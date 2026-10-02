"""Failure breaker for unattended work, and a cooldown for rate-limited providers.

Two small guards that keep unattended work (dispatch jobs, night shift tasks,
scheduled tasks) from hammering something that is broken.

Breaker
    After ``unattended_failure_breaker`` consecutive failed unattended runs
    (default 5; 0 = off) no NEW unattended run starts: the breaker is "open".
    The owner is told once through the notification bus (which push and the
    chat bridges already listen to). It closes by itself after
    ``unattended_breaker_cooldown_min`` minutes (default 30) or on a manual
    reset (``POST /api/budget/period/breaker/reset``). A run that is already
    going is never touched, and an interactive chat turn is never gated.

    A local model server answering garbage ("////"), which
    ``src/model_server_heal.py`` already detects, counts as one failure.

Cooldown
    A 429 or an overload answer (529, or a 503 that carries Retry-After) from a
    HOSTED endpoint puts that endpoint in cooldown for the provider's own
    Retry-After (``src.retry_policy.parse_retry_after``), or 15 minutes when it
    sent none. Fallback chains skip a cooled endpoint while a different one is
    available. Servers on this machine are never cooled down: that would lock
    the owner out of their own model.

The idea of pausing a worker for 15 minutes after a provider 429, and of
stopping new starts after a run of failures, is adapted from clodfarm
(MIT, Duke Security, Inc.). Everything here is stdlib and best-effort: no
function raises into the caller.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_S = 15 * 60.0
MAX_COOLDOWN_S = 24 * 3600.0
MIN_COOLDOWN_S = 1.0
#: Statuses that are a provider telling us to slow down. 503 only counts when
#: the provider also sent a Retry-After (a bare 503 is how a connect failure is
#: reported inside llm_core and says nothing about rate limits).
COOLDOWN_STATUSES = (429, 529)
FAILURE_KINDS = ("dispatch", "night_shift", "scheduled_task", "local_garbage")

_LOCK = threading.RLock()
_COOLDOWNS: Dict[str, Dict[str, Any]] = {}
_BREAKER: Dict[str, Any] = {
    "consecutive": 0, "open_since": None, "open_until": None, "opened": 0,
    "last_failure": None, "last_success": None, "auto_closed_at": None, "reset_at": None,
}
_NOTIFIED_OPEN = False
_LOADED = False
#: A final failed streaming attempt cannot name its endpoint (the retry
#: decision has no URL): it leaves the status here and the call's own trace
#: record, which does know the URL, picks it up in the same task.
_PENDING_STATUS: "contextvars.ContextVar[Optional[Tuple[int, Optional[float]]]]" = contextvars.ContextVar(
    "faustus_pending_provider_status", default=None)


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


def _int_setting(key: str, default: int) -> int:
    try:
        value = _setting(key, default)
        return int(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        return default


def _now() -> float:
    return time.time()


def _is_local(url: str) -> bool:
    try:
        from src.model_context import is_local_endpoint
        return bool(is_local_endpoint(url))
    except Exception:  # noqa: BLE001
        host = (urlparse(str(url or "")).hostname or "").lower()
        return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


# -- provider cooldown -------------------------------------------------------

def _host_of(url: str) -> str:
    text = str(url or "").strip()
    parsed = urlparse(text if "://" in text else "//" + text)
    host = (parsed.hostname or "").lower()
    if parsed.port and parsed.port not in (80, 443):
        host = f"{host}:{parsed.port}"
    return host


def _credential_fingerprint(headers: Optional[Mapping[str, Any]]) -> str:
    if not headers:
        return ""
    try:
        for name in ("Authorization", "authorization", "x-api-key", "X-Api-Key", "api-key"):
            value = headers.get(name)
            if value:
                return hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:8]
    except Exception:  # noqa: BLE001
        pass
    return ""


def endpoint_key(url: str) -> str:
    """``host[:port]`` of a provider URL: what a cooldown is keyed by."""
    return _host_of(url)


def _retry_after_of(headers: Optional[Mapping[str, Any]]) -> Optional[float]:
    if headers is None:
        return None
    try:
        from src.retry_policy import parse_retry_after
        return parse_retry_after(headers)
    except Exception:  # noqa: BLE001
        return None


def cooldown_seconds(status: Optional[int], headers: Optional[Mapping[str, Any]] = None, *,
                     retry_after: Optional[float] = None) -> Optional[float]:
    """How long a provider answer asks us to stay away, or None when it is not
    a rate-limit/overload answer. Honours Retry-After, else the default
    (``provider_cooldown_default_min``, 15; 0 turns the fallback cooldown off
    so only an explicit Retry-After cools an endpoint)."""
    if status is None:
        return None
    ra = retry_after if retry_after is not None else _retry_after_of(headers)
    if status not in COOLDOWN_STATUSES and not (status == 503 and ra is not None):
        return None
    if ra is not None:
        seconds = float(ra)
    else:
        default_min = _int_setting("provider_cooldown_default_min", 15)
        if default_min <= 0:
            return None
        seconds = default_min * 60.0
    return min(max(seconds, MIN_COOLDOWN_S), MAX_COOLDOWN_S)

def note_status(url: str, status: Optional[int], headers: Optional[Mapping[str, Any]] = None, *,
                request_headers: Optional[Mapping[str, Any]] = None,
                retry_after: Optional[float] = None, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Record a FINAL failed provider answer. Returns the cooldown it started,
    or None. Never raises."""
    try:
        if not url or _is_local(url):
            return None
        ra = retry_after if retry_after is not None else _retry_after_of(headers)
        seconds = cooldown_seconds(status, headers, retry_after=ra)
        if seconds is None:
            return None
        key = endpoint_key(url)
        if not key:
            return None
        t = _now() if now is None else now
        with _LOCK:
            prev = _COOLDOWNS.get(key) or {}
            active = (prev.get("until") or 0.0) > t
            entry = {
                "endpoint": key, "status": int(status),
                "since": prev.get("since") if active else t,
                "until": max(t + seconds, prev.get("until") or 0.0) if active else t + seconds,
                "retry_after_s": None if ra is None else round(float(ra), 1),
                "honoured_retry_after": ra is not None,
                "hits": (int(prev.get("hits") or 0) + 1) if active else 1,
                "credential": _credential_fingerprint(request_headers),
            }
            _COOLDOWNS[key] = entry
        logger.warning("[cooldown] %s answered %s: skipped for %.0fs", key, status, seconds)
        return dict(entry)
    except Exception:  # noqa: BLE001
        logger.debug("cooldown note failed", exc_info=True)
        return None


def note_pending_status(status: Optional[int], headers: Optional[Mapping[str, Any]] = None) -> None:
    """A streaming attempt failed for good with ``status``: remember it for the
    trace record of the same call (see :func:`take_pending_status`)."""
    try:
        ra = _retry_after_of(headers)
        if status in COOLDOWN_STATUSES or (status == 503 and ra is not None):
            _PENDING_STATUS.set((int(status), ra))
    except Exception:  # noqa: BLE001
        pass


def take_pending_status() -> Optional[Tuple[int, Optional[float]]]:
    try:
        value = _PENDING_STATUS.get()
        if value is not None:
            _PENDING_STATUS.set(None)
        return value
    except Exception:  # noqa: BLE001
        return None


def cooldown_for(url: str, headers: Optional[Mapping[str, Any]] = None, *,
                 now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The live cooldown covering ``url`` (and this credential), or None."""
    try:
        t = _now() if now is None else now
        key = endpoint_key(url)
        with _LOCK:
            entry = _COOLDOWNS.get(key)
            if not entry:
                return None
            if entry["until"] <= t:
                _COOLDOWNS.pop(key, None)
                return None
            mine = _credential_fingerprint(headers)
            theirs = entry.get("credential") or ""
            if mine and theirs and mine != theirs:
                return None  # another key at the same host has its own limit
            return dict(entry)
    except Exception:  # noqa: BLE001
        return None


def filter_candidates(candidates: Sequence[Any], descriptors: Optional[Sequence[Any]] = None, *,
                      now: Optional[float] = None):
    """Drop endpoints in cooldown from an ordered fallback chain.

    ``candidates`` are ``(url, model, headers)``. When every candidate is
    cooled down the chain is returned unchanged (a call that is likely to be
    refused still beats no call, and the provider may have lifted the limit).
    With ``descriptors`` the filtered pair is returned. Never raises.
    """
    cands = list(candidates or [])
    descs = list(descriptors) if descriptors is not None else None
    try:
        keep: List[int] = []
        for i, c in enumerate(cands):
            try:
                url, headers = c[0], (c[2] if len(c) > 2 else None)
            except Exception:  # noqa: BLE001
                keep.append(i)
                continue
            if cooldown_for(url, headers, now=now) is None:
                keep.append(i)
        if keep and len(keep) < len(cands):
            for i in range(len(cands)):
                if i not in keep:
                    logger.info("[cooldown] skipping %s in the fallback chain", endpoint_key(cands[i][0]))
            cands = [cands[i] for i in keep]
            if descs is not None:
                descs = [descs[i] for i in keep if i < len(descs)]
    except Exception:  # noqa: BLE001
        pass
    return (cands, descs) if descs is not None else cands


def cooldowns(now: Optional[float] = None) -> List[Dict[str, Any]]:
    t = _now() if now is None else now
    out: List[Dict[str, Any]] = []
    with _LOCK:
        for key, entry in list(_COOLDOWNS.items()):
            if entry["until"] <= t:
                _COOLDOWNS.pop(key, None)
                continue
            row = {k: v for k, v in entry.items() if k != "credential"}
            row["remaining_s"] = round(entry["until"] - t, 1)
            row["pause_until"] = entry["until"]
            out.append(row)
    return sorted(out, key=lambda r: r["until"])


def clear_cooldown(endpoint: Optional[str] = None) -> int:
    with _LOCK:
        if endpoint:
            key = _host_of(endpoint) or str(endpoint).lower()
            return 1 if _COOLDOWNS.pop(key, None) else 0
        n = len(_COOLDOWNS)
        _COOLDOWNS.clear()
        return n


# -- the breaker -------------------------------------------------------------

def threshold() -> int:
    return max(0, _int_setting("unattended_failure_breaker", 5))


def cooldown_minutes() -> int:
    return max(1, _int_setting("unattended_breaker_cooldown_min", 30))


def _state_path() -> str:
    try:
        from src.constants import DATA_DIR
    except Exception:  # noqa: BLE001
        DATA_DIR = "."
    return os.path.join(DATA_DIR, "unattended_breaker.json")


def _persist() -> None:
    try:
        path = _state_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_BREAKER, fh)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        logger.debug("breaker state not saved", exc_info=True)


def _load_once() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for k in _BREAKER:
                if k in data:
                    _BREAKER[k] = data[k]
    except (OSError, ValueError):
        pass


def _tick(now: float) -> None:
    """Close the breaker once its cooldown has passed (caller holds the lock)."""
    global _NOTIFIED_OPEN
    until = _BREAKER.get("open_until")
    if _BREAKER.get("open_since") and until and until <= now:
        _BREAKER.update(open_since=None, open_until=None, consecutive=0, auto_closed_at=now)
        _NOTIFIED_OPEN = False
        _persist()

def record(kind: str, ok: bool, detail: str = "", *, now: Optional[float] = None) -> Dict[str, Any]:
    """Book one finished unattended run (or a garbage reply from a local
    server). A success resets the run of failures; a failure that reaches the
    threshold opens the breaker. Returns :func:`status`. Never raises."""
    try:
        t = _now() if now is None else now
        opened_now = False
        with _LOCK:
            _load_once()
            _tick(t)
            if ok:
                _BREAKER["consecutive"] = 0
                _BREAKER["last_success"] = {"kind": kind, "at": t}
            else:
                _BREAKER["consecutive"] = int(_BREAKER.get("consecutive") or 0) + 1
                _BREAKER["last_failure"] = {"kind": kind, "at": t, "detail": str(detail or "")[:300]}
                limit = threshold()
                if limit > 0 and _BREAKER["consecutive"] >= limit and not _BREAKER.get("open_since"):
                    _BREAKER.update(open_since=t, open_until=t + cooldown_minutes() * 60.0,
                                    opened=int(_BREAKER.get("opened") or 0) + 1)
                    opened_now = True
            _persist()
        if opened_now:
            _notify_open()
        return status(now=t)
    except Exception:  # noqa: BLE001
        logger.debug("breaker record failed", exc_info=True)
        return {}


def _notify_open() -> None:
    """Tell the owner once, through the same bus push and the chat bridges use."""
    global _NOTIFIED_OPEN
    try:
        with _LOCK:
            if _NOTIFIED_OPEN:
                return
            _NOTIFIED_OPEN = True
            last = dict(_BREAKER.get("last_failure") or {})
            n = int(_BREAKER.get("consecutive") or 0)
            until = _BREAKER.get("open_until")
        from src import notifications
        notifications.emit(
            "unattended_breaker_open", owner=None,
            title="Unattended work paused",
            body=(f"{n} unattended runs failed in a row"
                  + (f" (last: {last.get('kind')}: {str(last.get('detail') or '')[:80]})" if last else "")
                  + f". No new unattended run starts for {cooldown_minutes()} min or until you reset it."),
            data={"consecutive": n, "open_until": until, "last_failure": last},
        )
    except Exception:  # noqa: BLE001
        logger.debug("breaker notification failed", exc_info=True)


def open_until(*, now: Optional[float] = None) -> Optional[float]:
    """When the breaker closes by itself, or None when it is closed."""
    try:
        t = _now() if now is None else now
        with _LOCK:
            _load_once()
            _tick(t)
            if _BREAKER.get("open_since") and threshold() > 0:
                return float(_BREAKER.get("open_until") or t + 60.0)
            return None
    except Exception:  # noqa: BLE001
        return None


def is_open(*, now: Optional[float] = None) -> bool:
    return open_until(now=now) is not None


def check(*, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """None when unattended work may start, else the ``breaker_open`` answer."""
    t = _now() if now is None else now
    until = open_until(now=t)
    if until is None:
        return None
    with _LOCK:
        last = dict(_BREAKER.get("last_failure") or {})
        n = int(_BREAKER.get("consecutive") or 0)
    return {
        "status": "breaker_open", "breaker_open": True, "kind": "breaker",
        "reason": (f"{n} unattended runs failed in a row"
                   + (f" (last: {last.get('kind')}: {str(last.get('detail') or '')[:120]})" if last else "")
                   + "; new unattended runs wait until the breaker closes or is reset"),
        "pause_until": until, "retry_after_s": max(1.0, round(until - t, 1)), "workers": 0,
    }


def reset(*, now: Optional[float] = None) -> Dict[str, Any]:
    """Manual reset: close the breaker and forget the run of failures."""
    global _NOTIFIED_OPEN
    t = _now() if now is None else now
    with _LOCK:
        _load_once()
        _BREAKER.update(open_since=None, open_until=None, consecutive=0, reset_at=t)
        _NOTIFIED_OPEN = False
        _persist()
    return status(now=t)


def status(*, now: Optional[float] = None) -> Dict[str, Any]:
    t = _now() if now is None else now
    until = open_until(now=t)
    with _LOCK:
        return {
            "enabled": threshold() > 0, "threshold": threshold(), "cooldown_min": cooldown_minutes(),
            "open": until is not None, "consecutive_failures": int(_BREAKER.get("consecutive") or 0),
            "open_since": _BREAKER.get("open_since") if until is not None else None,
            "pause_until": until, "opened_count": int(_BREAKER.get("opened") or 0),
            "last_failure": _BREAKER.get("last_failure"), "last_success": _BREAKER.get("last_success"),
            "auto_closed_at": _BREAKER.get("auto_closed_at"), "reset_at": _BREAKER.get("reset_at"),
        }


def reset_for_tests() -> None:
    global _NOTIFIED_OPEN, _LOADED
    with _LOCK:
        _LOADED = True   # a test starts clean: never read a previous test's saved state
        _COOLDOWNS.clear()
        _BREAKER.update(consecutive=0, open_since=None, open_until=None, opened=0, last_failure=None,
                        last_success=None, auto_closed_at=None, reset_at=None)
        _NOTIFIED_OPEN = False
        _LOADED = True  # never read a previous run's file
    try:
        _PENDING_STATUS.set(None)
    except Exception:  # noqa: BLE001
        pass