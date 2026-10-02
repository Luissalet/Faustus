"""Period budget governor: spend and GPU time paced over a day or a week.

Until now Faustus only capped a single turn or run (``agent_turn_max_cost_usd``,
``agent_budget_tokens_per_run``, ``src/budget_account.py``); ``src/usage_recap.py``
only reads. Nothing spread an allowance over a day or a week, so a night of
unattended work could spend the week's money before Monday, or keep the GPU
busy right up to the moment the owner sat down.

This module adds the missing layer, for UNATTENDED work and for fan-out:

* a pure decision, :func:`decide`, on a pace line: the share of a window's
  allowance that may have been used by now is ``elapsed + band`` (the budget is
  1.0). Above the line the work waits until the line catches up and the answer
  says when (``pause_until``) and why. A floor (``min_workers``) keeps pacing
  from freezing everything early in a window: pacing slows work down, only a
  window that reached its target stops it. A window whose reset has passed is
  empty (a new window never inherits the old one's spend);
* a ledger, per provider/endpoint and per window (``day`` or ``week``): hosted
  spend from the real ``usage.cost_usd`` / token counts the call reported, and
  ``gpu_seconds`` for the local model (the seconds the local generation slot
  was held), fed by ``src/llm_trace.py`` and ``src/llm_core.py``;
* back-off for the owner: while an interactive chat turn is live, unattended
  work waits. A running interactive turn is never stopped by anything here;
* enforcement points that return one ``budget_paused`` answer with
  ``pause_until`` and ``reason``: a dispatch job (``src/dispatch.py``), between
  night shift tasks (``src/night_shift.py``), a paid escalation of the model
  router (``src/model_router.py``) and the number of sub-agents
  ``delegate_agents`` may start.

Everything is off by default except the interactive back-off: a provider with
no target and ``budget_gpu_daily_seconds`` 0 are never limited.

Adapted from clodfarm (MIT, Duke Security, Inc.): the pace line, the floor, and
"a window that reset counts as empty" come from its ``governor.py``; the money,
GPU-seconds and interactive back-off sources are Faustus's own.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

WINDOWS = ("day", "week")
DEFAULT_WINDOW = "week"
DEFAULT_BAND_PCT = 10
#: How often an interactive back-off tells a waiting caller to look again.
INTERACTIVE_RETRY_S = 30.0
_MIN_PAUSE_S = 60.0
_KEEP_DAYS = 40


class BudgetPaused(Exception):
    """An enforcement point refused to start work; ``info`` is the answer."""

    def __init__(self, info: Dict[str, Any]):
        super().__init__(str(info.get("reason") or "budget paused"))
        self.info = dict(info)


# -- settings ----------------------------------------------------------------

def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "" or isinstance(value, bool):
            return default
        out = float(value)
        return out if out == out and out not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError):
        return default


def period_window() -> str:
    value = str(_setting("budget_period_window", DEFAULT_WINDOW) or DEFAULT_WINDOW).strip().lower()
    return value if value in WINDOWS else DEFAULT_WINDOW


def band_fraction() -> float:
    pct = _num(_setting("budget_period_band_pct", DEFAULT_BAND_PCT), DEFAULT_BAND_PCT)
    return min(max(pct, 0.0), 100.0) / 100.0


def gpu_daily_seconds() -> float:
    return max(0.0, _num(_setting("budget_gpu_daily_seconds", 0), 0.0))


def backoff_when_interactive() -> bool:
    value = _setting("budget_backoff_when_interactive", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def min_workers() -> int:
    return max(0, int(_num(_setting("budget_period_min_workers", 0), 0)))


def targets() -> Dict[str, Dict[str, float]]:
    """``budget_period_targets`` as ``{provider: {"usd": x, "tokens": n}}``.

    The setting is a JSON object (or a JSON string of one). A bare number is
    dollars. A key is a host (``openrouter.ai``), a provider kind
    (``openrouter``, ``anthropic``) or ``*`` for every hosted provider together.
    """
    raw = _setting("budget_period_targets", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raw = {}
    out: Dict[str, Dict[str, float]] = {}
    if not isinstance(raw, Mapping):
        return out
    for key, value in raw.items():
        name = str(key or "").strip().lower()
        if not name:
            continue
        if isinstance(value, Mapping):
            usd = _num(value.get("usd", value.get("dollars")), 0.0)
            tokens = _num(value.get("tokens"), 0.0)
        else:
            usd, tokens = _num(value, 0.0), 0.0
        if usd > 0 or tokens > 0:
            out[name] = {k: v for k, v in (("usd", usd), ("tokens", tokens)) if v > 0}
    return out


# -- windows -----------------------------------------------------------------

def window_bounds(window: str, now: Optional[float] = None) -> Tuple[float, float]:
    """``(start, end)`` epoch seconds of the CURRENT window, in local time:
    ``day`` runs midnight to midnight, ``week`` Monday to Monday."""
    t = time.time() if now is None else now
    local = datetime.fromtimestamp(t)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "week":
        start = start - timedelta(days=start.weekday())
        end = start + timedelta(days=7)
    else:
        end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def elapsed_fraction(start: float, end: float, now: float) -> float:
    length = max(end - start, 1.0)
    return min(max((now - start) / length, 0.0), 1.0)


def live_usage(usage: float, resets_at: Optional[float], now: float) -> float:
    """A window whose reset time has passed starts again from zero."""
    if resets_at is not None and resets_at <= now:
        return 0.0
    return max(float(usage or 0.0), 0.0)


# -- the pure decision -------------------------------------------------------

@dataclass
class Decision:
    workers: int
    reason: str
    kind: str = "ok"                       # ok | pace | limit | interactive | cooldown | breaker
    pause_until: Optional[float] = None    # set when workers == 0 and we know when to look again
    retry_after_s: Optional[float] = None
    scope: str = ""                        # which window/provider decided
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.workers > 0

    def to_dict(self) -> Dict[str, Any]:
        out = {"workers": self.workers, "reason": self.reason, "kind": self.kind,
               "pause_until": self.pause_until, "retry_after_s": self.retry_after_s,
               "scope": self.scope, "details": self.details}
        if self.pause_until:
            out["pause_until_iso"] = datetime.fromtimestamp(self.pause_until).isoformat(timespec="seconds")
        return out


def decide(window_usage: float, target: float, band: float, elapsed_fraction: float,
           min_workers: int = 1, interactive_active: bool = False, *, max_workers: int = 1,
           window_start: Optional[float] = None, window_length: Optional[float] = None,
           now: Optional[float] = None, label: str = "window") -> Decision:
    """How many workers may start now, against one window's allowance.

    ``window_usage`` and ``target`` share a unit (dollars, tokens or seconds);
    ``band`` is the head start as a fraction of the target (0.10 = 10 %);
    ``elapsed_fraction`` is how much of the window has gone (0..1). With
    ``window_start``/``window_length`` the answer can say when to look again.

    1. no target (<= 0): never limited;
    2. hard stop: the window reached its target, wait for its reset;
    3. interactive back-off: the owner is working, wait a little;
    4. pace line ``allowed = elapsed + band`` (the target is 1.0): under it,
       full speed; inside the band, partial; above it, wait until the line
       catches up. ``min_workers`` keeps that last case from freezing work.
    """
    n = max(int(max_workers or 0), 0)
    t = time.time() if now is None else now
    if n == 0:
        return Decision(0, f"{label}: max_workers is 0", "limit", scope=label)
    window_end = (window_start + window_length) if (window_start is not None and window_length) else None
    usage = max(_num(window_usage), 0.0)
    target = _num(target)
    interactive = Decision(0, f"{label}: an interactive turn is running; waiting for it", "interactive",
                           retry_after_s=INTERACTIVE_RETRY_S, scope=label)
    if target <= 0:
        return interactive if interactive_active else Decision(n, f"{label}: no target set", scope=label)
    band = min(max(_num(band), 0.0), 1.0)
    elapsed = min(max(_num(elapsed_fraction), 0.0), 1.0)
    used_frac = usage / target
    allowed_frac = min(1.0, elapsed + band)
    details = {"used": round(usage, 6), "target": target, "used_fraction": round(used_frac, 4),
               "elapsed_fraction": round(elapsed, 4), "allowed_fraction": round(allowed_frac, 4),
               "band": band}
    if usage >= target:
        return Decision(0, f"{label}: {used_frac:.0%} of the target used", "limit", pause_until=window_end,
                        retry_after_s=None if window_end is None else max(window_end - t, 0.0),
                        scope=label, details=details)
    if interactive_active:
        interactive.details = details
        return interactive
    headroom = allowed_frac - used_frac
    if headroom <= 0:
        frac = 0.0
    else:
        frac = 1.0 if band <= 0 else min(headroom / band, 1.0)
        if frac > 1.0 - 1e-9:
            frac = 1.0
    workers = math.ceil(n * frac - 1e-9) if frac > 0 else 0
    floor = min(max(int(min_workers or 0), 0), n)
    reason = (f"{label}: pacing, {used_frac:.0%} used of a {allowed_frac:.0%} allowance so far"
              if frac < 1 else f"{label}: within the pace line ({used_frac:.0%} used, {allowed_frac:.0%} allowed)")
    details["pace"] = round(frac, 3)
    if workers < floor:
        # Pacing spreads the allowance out; it is not a stop. Only a window at
        # its target stops work, so early use never freezes the whole queue.
        return Decision(floor, reason + f" (keeping {floor})", "pace", scope=label, details=details)
    if workers == 0:
        resume = None
        if window_start is not None and window_length:
            need = used_frac - band            # elapsed at which elapsed + band reaches the usage
            resume = min(max(window_start + need * window_length, t + _MIN_PAUSE_S), window_end)
        if resume is None:
            resume = t + 300.0
        return Decision(0, reason, "pace", pause_until=resume, retry_after_s=max(resume - t, 0.0),
                        scope=label, details=details)
    return Decision(workers, reason, "pace" if frac < 1 else "ok", scope=label, details=details)

# -- the ledger --------------------------------------------------------------

_DB_LOCK = threading.Lock()
_DB_PATH_OVERRIDE: Optional[str] = None
_PRUNED_AT = 0.0


def set_db_path(path: Optional[str]) -> None:
    global _DB_PATH_OVERRIDE
    _DB_PATH_OVERRIDE = path


def db_path() -> str:
    if _DB_PATH_OVERRIDE:
        return _DB_PATH_OVERRIDE
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "period_budget.sqlite3")


def _connect() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS usage ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, provider TEXT NOT NULL,"
        " kind TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',"
        " usd REAL NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,"
        " unpriced_tokens INTEGER NOT NULL DEFAULT 0, gpu_seconds REAL NOT NULL DEFAULT 0)")
    conn.execute("CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts)")
    return conn


def provider_of(url: str) -> Tuple[str, str]:
    """``(host, kind)`` of an endpoint: ``("local", "local")`` for a server on
    this machine, else the lower-cased host and the provider kind llm_core
    detects (``openrouter``, ``anthropic``, ``openai`` ...)."""
    text = str(url or "").strip()
    try:
        from src.model_context import is_local_endpoint
        if text and is_local_endpoint(text):
            return "local", "local"
    except Exception:  # noqa: BLE001
        pass
    host = (urlparse(text if "://" in text else "//" + text).hostname or "").lower() or "unknown"
    kind = ""
    try:
        from src.llm_core import _detect_provider
        kind = str(_detect_provider(text) or "")
    except Exception:  # noqa: BLE001
        kind = ""
    return host, kind


def _insert(provider: str, kind: str, model: str, usd: float, tokens: int, unpriced: int,
            gpu_seconds: float, ts: Optional[float]) -> None:
    global _PRUNED_AT
    t = time.time() if ts is None else ts
    with _DB_LOCK:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO usage(ts, provider, kind, model, usd, tokens, unpriced_tokens, gpu_seconds)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (t, provider, kind, str(model or "")[:120], usd, tokens, unpriced, gpu_seconds))
            if t - _PRUNED_AT > 3600:
                _PRUNED_AT = t
                conn.execute("DELETE FROM usage WHERE ts < ?", (t - _KEEP_DAYS * 86400,))
            conn.commit()
        finally:
            conn.close()


def record_usage(provider: str, *, kind: str = "", model: str = "", usd: Optional[float] = None,
                 tokens: int = 0, gpu_seconds: float = 0.0, ts: Optional[float] = None) -> None:
    """Book one call's spend. ``usd`` None means the provider reported no
    price: the tokens are counted as unpriced rather than as free."""
    try:
        tokens = max(int(tokens or 0), 0)
        priced = usd is not None and _num(usd, -1.0) >= 0
        _insert(str(provider or "unknown").lower(), str(kind or "").lower(), model,
                float(usd) if priced else 0.0, tokens, 0 if priced else tokens,
                max(_num(gpu_seconds), 0.0), ts)
    except Exception:  # noqa: BLE001
        logger.debug("period ledger write failed", exc_info=True)


def on_model_call(url: str, model: str, usage: Optional[Mapping[str, Any]], *,
                  request_headers: Optional[Mapping[str, Any]] = None, error: Optional[str] = None) -> None:
    """The hook ``src/llm_trace.record_call`` runs for every model call.

    A hosted call books its tokens and reported price; a local call books
    nothing here (its GPU seconds come from the generation slot). A final
    429/overload noted by the call starts that endpoint's cooldown. Never
    raises."""
    try:
        from src import unattended_breaker
        pending = unattended_breaker.take_pending_status()
        if pending is not None and url:
            unattended_breaker.note_status(url, pending[0], retry_after=pending[1],
                                           request_headers=request_headers)
        host, kind = provider_of(url)
        if host == "local" or not url:
            return
        u = dict(usage or {})
        try:
            from src.turn_spend import tokens_from_usage
            tokens = tokens_from_usage(u) or 0
        except Exception:  # noqa: BLE001
            tokens = int(_num(u.get("total_tokens")) or (_num(u.get("input_tokens")) + _num(u.get("output_tokens"))))
        if not tokens:        # an un-normalised OpenAI-shaped usage block
            tokens = int(_num(u.get("prompt_tokens")) + _num(u.get("completion_tokens")))
        cost = u.get("cost_usd")
        priced = isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0
        if not tokens and not priced:
            return
        record_usage(host, kind=kind, model=model, usd=float(cost) if priced else None, tokens=tokens)
    except Exception:  # noqa: BLE001
        logger.debug("period ledger hook failed", exc_info=True)


def record_gpu_seconds(url: str, seconds: float, model: str = "") -> None:
    """Seconds the local generation slot was held (the caller times it)."""
    try:
        if seconds and seconds > 0:
            record_usage("local", kind="local", model=model, gpu_seconds=float(seconds))
    except Exception:  # noqa: BLE001
        pass


def _match_clause(key: str) -> Tuple[str, List[Any]]:
    key = str(key or "").strip().lower()
    if key in ("*", "all", "hosted", "cloud"):
        return "provider != 'local'", []
    if key == "local":
        return "provider = 'local'", []
    return "(provider = ? OR kind = ? OR provider LIKE ?)", [key, key, "%" + key]


def usage_since(key: str, since: float, until: Optional[float] = None) -> Dict[str, float]:
    """What ``key`` (``*``, ``local``, a host or a provider kind) used since ``since``."""
    clause, params = _match_clause(key)
    sql = ("SELECT COALESCE(SUM(usd),0), COALESCE(SUM(tokens),0), COALESCE(SUM(unpriced_tokens),0),"
           " COALESCE(SUM(gpu_seconds),0), COUNT(*) FROM usage WHERE ts >= ? AND " + clause)
    args: List[Any] = [since] + params
    if until is not None:
        sql += " AND ts < ?"
        args.append(until)
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                row = conn.execute(sql, args).fetchone()
            finally:
                conn.close()
    except Exception:  # noqa: BLE001
        logger.debug("period ledger read failed", exc_info=True)
        row = (0, 0, 0, 0, 0)
    return {"usd": float(row[0]), "tokens": float(row[1]), "unpriced_tokens": float(row[2]),
            "gpu_seconds": float(row[3]), "calls": float(row[4])}


def providers_seen(since: float) -> List[Dict[str, Any]]:
    """Every provider with spend since ``since``, for the state view."""
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT provider, MAX(kind), SUM(usd), SUM(tokens), SUM(unpriced_tokens), SUM(gpu_seconds), COUNT(*)"
                    " FROM usage WHERE ts >= ? GROUP BY provider ORDER BY SUM(usd) DESC", (since,)).fetchall()
            finally:
                conn.close()
    except Exception:  # noqa: BLE001
        return []
    return [{"provider": r[0], "kind": r[1], "usd": round(float(r[2] or 0), 6), "tokens": int(r[3] or 0),
             "unpriced_tokens": int(r[4] or 0), "gpu_seconds": round(float(r[5] or 0), 2), "calls": int(r[6])}
            for r in rows]


def reset_for_tests() -> None:
    global _PRUNED_AT
    _PRUNED_AT = 0.0
    try:
        if os.path.exists(db_path()):
            with _DB_LOCK:
                conn = _connect()
                try:
                    conn.execute("DELETE FROM usage")
                    conn.commit()
                finally:
                    conn.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        from src import unattended_breaker
        unattended_breaker.reset_for_tests()
    except Exception:  # noqa: BLE001
        pass

# -- reading the world -------------------------------------------------------

def interactive_active() -> bool:
    """True while an interactive chat turn is live (``src/interactive_gate``
    watches the detached chat runs), and the back-off is on. A dispatch worker,
    a scheduled task or a chat-bridge turn is not interactive."""
    if not backoff_when_interactive():
        return False
    try:
        from src import interactive_gate
        return bool(interactive_gate._has_active_chat_stream())
    except Exception:  # noqa: BLE001
        return False


def _hosted_decisions(url: Optional[str], *, interactive: bool, max_workers: int, now: float,
                      floor: int) -> List[Decision]:
    """One decision per configured target that covers ``url`` (``None`` = every hosted target)."""
    tg = targets()
    if not tg:
        return []
    window = period_window()
    start, end = window_bounds(window, now)
    elapsed = elapsed_fraction(start, end, now)
    band = band_fraction()
    host, kind = provider_of(url) if url else ("", "")
    out: List[Decision] = []
    for key, limits in tg.items():
        if url:
            covers = key in ("*", "all", "hosted", "cloud") or key in (host, kind) or (host and host.endswith(key))
            if not covers:
                continue
        used = usage_since(key, start, end)
        for metric in ("usd", "tokens"):
            if metric not in limits:
                continue
            d = decide(live_usage(used[metric], end, now), limits[metric], band, elapsed, floor, interactive,
                       max_workers=max_workers, window_start=start, window_length=end - start, now=now,
                       label=f"{key} {metric}/{window}")
            d.details.update(provider=key, metric=metric, window=window)
            out.append(d)
    return out


def _gpu_decision(*, interactive: bool, max_workers: int, now: float, floor: int) -> Optional[Decision]:
    target = gpu_daily_seconds()
    if target <= 0:
        return None
    start, end = window_bounds("day", now)
    used = usage_since("local", start, end)["gpu_seconds"]
    d = decide(live_usage(used, end, now), target, band_fraction(), elapsed_fraction(start, end, now), floor,
               interactive, max_workers=max_workers, window_start=start, window_length=end - start, now=now,
               label="local gpu_seconds/day")
    d.details.update(provider="local", metric="gpu_seconds", window="day")
    return d


_KIND_ORDER = {"breaker": 0, "limit": 1, "cooldown": 2, "pace": 3, "interactive": 4, "ok": 5}


def _most_restrictive(decisions: List[Decision]) -> Decision:
    return sorted(decisions, key=lambda d: (d.workers, _KIND_ORDER.get(d.kind, 9), -(d.pause_until or 0)))[0]


def evaluate(url: Optional[str] = None, *, unattended: bool = False, max_workers: int = 1,
             now: Optional[float] = None, hosted_any: bool = False) -> Decision:
    """Decide for one start: the most restrictive of every window that covers
    ``url`` (its hosted targets, or the local GPU-seconds window), the
    endpoint's cooldown, the failure breaker (unattended only) and the
    interactive back-off (unattended only). With no ``url`` every window is
    read; ``hosted_any`` reads every hosted target (a paid escalation does not
    know which provider it will use yet)."""
    t = time.time() if now is None else now
    n = max(int(max_workers or 0), 1)
    floor = min(min_workers(), n)
    interactive = bool(unattended) and interactive_active()
    decisions: List[Decision] = []
    breaker_api = None
    try:
        from src import unattended_breaker as breaker_api  # noqa: F811
    except Exception:  # noqa: BLE001
        breaker_api = None
    if unattended and breaker_api is not None:
        blocked = breaker_api.check(now=t)
        if blocked:
            decisions.append(Decision(0, blocked["reason"], "breaker", pause_until=blocked["pause_until"],
                                      retry_after_s=blocked["retry_after_s"], scope="breaker"))
    host = provider_of(url)[0] if url else ""
    if url and host != "local" and breaker_api is not None:
        cd = breaker_api.cooldown_for(url, now=t)
        if cd:
            decisions.append(Decision(
                0, f"{cd['endpoint']} is in cooldown after HTTP {cd['status']}", "cooldown",
                pause_until=cd["until"], retry_after_s=max(cd["until"] - t, 0.0), scope=f"cooldown {cd['endpoint']}"))
    if url and host == "local":
        g = _gpu_decision(interactive=interactive, max_workers=n, now=t, floor=floor)
        if g is not None:
            decisions.append(g)
    elif url:
        decisions.extend(_hosted_decisions(url, interactive=interactive, max_workers=n, now=t, floor=floor))
    else:
        if not hosted_any:
            g = _gpu_decision(interactive=interactive, max_workers=n, now=t, floor=floor)
            if g is not None:
                decisions.append(g)
        decisions.extend(_hosted_decisions(None, interactive=interactive, max_workers=n, now=t, floor=floor))
    if interactive and not any(d.kind == "interactive" for d in decisions):
        decisions.append(Decision(0, "an interactive turn is running; unattended work waits for it", "interactive",
                                  retry_after_s=INTERACTIVE_RETRY_S, scope="interactive"))
    if not decisions:
        return Decision(n, "no period budget applies", scope="none")
    return _most_restrictive(decisions)


def paused_answer(decision: Decision, *, point: str = "") -> Dict[str, Any]:
    """The ``budget_paused`` answer every enforcement point returns."""
    d = decision.to_dict()
    kind = decision.kind
    return {"status": "breaker_open" if kind == "breaker" else "budget_paused",
            "budget_paused": True, "breaker_open": kind == "breaker",
            "point": point, "kind": kind, "scope": decision.scope, "reason": decision.reason,
            "pause_until": d.get("pause_until"), "pause_until_iso": d.get("pause_until_iso"),
            "retry_after_s": None if decision.retry_after_s is None else round(decision.retry_after_s, 1),
            "details": decision.details}


def gate(point: str, url: Optional[str] = None, *, unattended: bool = False, max_workers: int = 1,
         now: Optional[float] = None, hosted_any: bool = False) -> Optional[Dict[str, Any]]:
    """None when work may start, else the ``budget_paused`` answer. Never
    raises: a broken ledger lets the work through (a budget guard that fails
    closed would be an outage)."""
    try:
        d = evaluate(url, unattended=unattended, max_workers=max_workers, now=now, hosted_any=hosted_any)
        if d.workers <= 0:
            return paused_answer(d, point=point)
    except Exception:  # noqa: BLE001
        logger.debug("period budget gate failed open", exc_info=True)
    return None


def allowed_workers(point: str, url: Optional[str], wanted: int, *, unattended: bool = False,
                    now: Optional[float] = None) -> Tuple[int, Optional[Dict[str, Any]]]:
    """How many of ``wanted`` workers may start, and the answer when fewer
    (or none) may."""
    wanted = max(int(wanted or 0), 1)
    try:
        d = evaluate(url, unattended=unattended, max_workers=wanted, now=now)
        if d.workers <= 0:
            return 0, paused_answer(d, point=point)
        return min(d.workers, wanted), (paused_answer(d, point=point) if d.workers < wanted else None)
    except Exception:  # noqa: BLE001
        logger.debug("period budget worker cap failed open", exc_info=True)
        return wanted, None

def gate_paid_escalation(now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The model router's check before it offers a paid escalation: blocked
    when the all-hosted target is spent, or when every per-provider target is."""
    try:
        t = time.time() if now is None else now
        decisions = _hosted_decisions(None, interactive=False, max_workers=1, now=t, floor=0)
        if not decisions:
            return None
        total = [d for d in decisions if d.details.get("provider") in ("*", "all", "hosted", "cloud")]
        spent_total = [d for d in total if d.workers <= 0]
        if spent_total:
            return paused_answer(_most_restrictive(spent_total), point="router_escalation")
        specific = [d for d in decisions if d not in total]
        if specific and all(d.workers <= 0 for d in specific):
            return paused_answer(_most_restrictive(specific), point="router_escalation")
    except Exception:  # noqa: BLE001
        logger.debug("period budget escalation gate failed open", exc_info=True)
    return None


def wait_seconds(answer: Optional[Mapping[str, Any]], *, now: Optional[float] = None,
                 ceiling: float = 300.0) -> float:
    """How long a caller that chooses to WAIT (a night shift on an interactive
    back-off) should sleep before asking again."""
    if not answer:
        return 0.0
    t = time.time() if now is None else now
    retry = answer.get("retry_after_s")
    if retry is None and answer.get("pause_until"):
        retry = float(answer["pause_until"]) - t
    return min(max(_num(retry, INTERACTIVE_RETRY_S), 1.0), ceiling)


# -- state for the API -------------------------------------------------------

def state(now: Optional[float] = None) -> Dict[str, Any]:
    """Everything ``GET /api/budget/period`` shows: per provider and window the
    use, the target, the pace line and when it unpauses; the local GPU window;
    the breaker; the cooldowns; whether an interactive turn is live."""
    t = time.time() if now is None else now
    window = period_window()
    start, end = window_bounds(window, t)
    elapsed = elapsed_fraction(start, end, t)
    band = band_fraction()
    tg = targets()
    rows: List[Dict[str, Any]] = []
    for key, limits in tg.items():
        used = usage_since(key, start, end)
        for metric in ("usd", "tokens"):
            if metric not in limits:
                continue
            d = decide(live_usage(used[metric], end, t), limits[metric], band, elapsed, min_workers(), False,
                       max_workers=1, window_start=start, window_length=end - start, now=t,
                       label=f"{key} {metric}/{window}")
            allowed_frac = min(1.0, elapsed + band)
            rows.append({
                "provider": key, "window": window, "metric": metric, "used": round(used[metric], 6),
                "target": limits[metric],
                "pace": {"elapsed_fraction": round(elapsed, 4), "allowed_fraction": round(allowed_frac, 4),
                         "allowed": round(limits[metric] * allowed_frac, 6), "band": band},
                "paused": d.workers <= 0, "paused_until": d.pause_until, "reason": d.reason, "kind": d.kind,
                "window_start": start, "window_resets_at": end,
                "unpriced_tokens": int(used["unpriced_tokens"]), "calls": int(used["calls"])})
    gpu = None
    target = gpu_daily_seconds()
    dstart, dend = window_bounds("day", t)
    gused = usage_since("local", dstart, dend)
    if target > 0 or gused["gpu_seconds"] > 0:
        d = decide(live_usage(gused["gpu_seconds"], dend, t), target, band, elapsed_fraction(dstart, dend, t),
                   min_workers(), False, max_workers=1, window_start=dstart, window_length=dend - dstart, now=t,
                   label="local gpu_seconds/day")
        gpu = {"provider": "local", "window": "day", "metric": "gpu_seconds",
               "used": round(gused["gpu_seconds"], 2), "target": target or None,
               "paused": target > 0 and d.workers <= 0,
               "paused_until": d.pause_until if target > 0 else None,
               "reason": d.reason if target > 0 else "no target set",
               "pace": {"elapsed_fraction": round(elapsed_fraction(dstart, dend, t), 4),
                        "allowed_fraction": round(min(1.0, elapsed_fraction(dstart, dend, t) + band), 4),
                        "allowed": round(target * min(1.0, elapsed_fraction(dstart, dend, t) + band), 2) if target else None,
                        "band": band},
               "window_start": dstart, "window_resets_at": dend, "calls": int(gused["calls"])}
    try:
        from src import unattended_breaker
        breaker = unattended_breaker.status(now=t)
        cools = unattended_breaker.cooldowns(t)
    except Exception:  # noqa: BLE001
        breaker, cools = {}, []
    return {
        "now": t, "window": window, "window_start": start, "window_resets_at": end,
        "enabled": bool(tg) or gpu_daily_seconds() > 0,
        "settings": {"budget_period_window": window, "budget_period_band_pct": int(round(band * 100)),
                     "budget_gpu_daily_seconds": gpu_daily_seconds(),
                     "budget_backoff_when_interactive": backoff_when_interactive(),
                     "budget_period_targets": tg},
        "interactive_active": interactive_active(), "providers": rows, "gpu": gpu,
        "hosted_seen": [p for p in providers_seen(start) if p["provider"] != "local"],
        "breaker": breaker, "cooldowns": cools,
    }

def _fmt_amount(metric: str, value: Any) -> str:
    v = _num(value)
    if metric == "usd":
        return f"${v:,.4f}".rstrip("0").rstrip(".") if v < 1 else f"${v:,.2f}"
    if metric == "gpu_seconds":
        return f"{v:,.0f} s"
    return f"{v:,.0f}"


def render_text(data: Mapping[str, Any]) -> str:
    """The state as a few readable lines (for the agent and the MCP tool)."""
    lines = ["### Period budget"]
    if not data.get("enabled"):
        lines.append("- no target set: nothing is paced (set `budget_period_targets` or `budget_gpu_daily_seconds`)")
    for row in data.get("providers") or []:
        flag = f"PAUSED until {datetime.fromtimestamp(row['paused_until']).isoformat(timespec='minutes')}" \
            if row.get("paused") and row.get("paused_until") else ("PAUSED" if row.get("paused") else "ok")
        lines.append(f"- {row['provider']} {row['metric']}/{row['window']}: {_fmt_amount(row['metric'], row['used'])} of "
                     f"{_fmt_amount(row['metric'], row['target'])} (pace line {_fmt_amount(row['metric'], row['pace']['allowed'])}) - {flag}")
    gpu = data.get("gpu")
    if gpu:
        flag = f"PAUSED until {datetime.fromtimestamp(gpu['paused_until']).isoformat(timespec='minutes')}" \
            if gpu.get("paused") and gpu.get("paused_until") else ("PAUSED" if gpu.get("paused") else "ok")
        target = _fmt_amount("gpu_seconds", gpu["target"]) if gpu.get("target") else "no target"
        lines.append(f"- local GPU/day: {_fmt_amount('gpu_seconds', gpu['used'])} of {target} - {flag}")
    if data.get("interactive_active"):
        lines.append("- an interactive turn is live: unattended work waits")
    br = data.get("breaker") or {}
    if br.get("open"):
        lines.append(f"- failure breaker OPEN after {br.get('consecutive_failures')} failures, until "
                     f"{datetime.fromtimestamp(br['pause_until']).isoformat(timespec='minutes')}")
    elif br.get("enabled"):
        lines.append(f"- failure breaker closed ({br.get('consecutive_failures', 0)}/{br.get('threshold')} failures in a row)")
    for cd in data.get("cooldowns") or []:
        lines.append(f"- {cd['endpoint']} in cooldown after HTTP {cd['status']} for another {cd['remaining_s']:.0f} s")
    return "\n".join(lines)