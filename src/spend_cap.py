"""spend_cap.py - a default-on DAILY hard cap on spend with PAID remote providers.

Why: an unattended loop, a runaway sub-agent fan-out or a long research turn can
spend real money on a paid endpoint while nobody watches. The per-turn ceilings
(src/autonomy_budget.py, agent_turn_max_cost_usd) bound one turn; this bounds
the DAY, across every turn, task, sub-agent, advisor and utility call.

Design
------
* One accumulator, persisted to DATA_DIR/spend_cap.json and keyed by the LOCAL
  calendar date. A new date starts from zero (the old day is simply dropped).
* One choke point: ``src/llm_core.py`` calls :func:`gate` before it sends a
  request (``llm_call``, ``llm_call_async``, ``stream_llm``) and :func:`record`
  with the provider-reported usage once it has it. Everything that reaches a
  model through those three functions is covered: the main loop, sub-agents,
  the advisor, typed decisions, compaction, research, memory upkeep, workflows.
* A call counts only when its endpoint is PAID: not a local/private/tailscale
  address, not a configured ``local`` endpoint (llama-server, Ollama, Sparks),
  not a ``faustus-cli://`` client. Local models are never affected.
* Spend is the provider's real ``cost_usd`` when it reports one. A paid call
  without a cost counts its tokens only and is tallied as ``unknown_cost_calls``
  (never booked as free dollars). The token cap therefore still bites on
  providers that never report a price.
* When the day's total has reached a limit, :func:`gate` raises
  :class:`SpendCapReached` (HTTP 429, ``error_class = "spend_cap.reached"``)
  before the request goes out, and sends ONE notification for that day. Calls
  already in flight finish and are booked; nothing is cut mid-stream.

Settings: ``paid_spend_cap_enabled`` (default true), ``paid_spend_cap_daily_usd``
(default 5.0; 0 = no dollar cap), ``paid_spend_cap_daily_tokens`` (default
2,000,000; 0 = no token cap).

Every bookkeeping failure is swallowed: this module never breaks a model call
except by the deliberate refusal.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import HTTPException

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(DATA_DIR, "spend_cap.json")
ERROR_CLASS = "spend_cap.reached"

DEFAULT_ENABLED = True
DEFAULT_DAILY_USD = 5.0
DEFAULT_DAILY_TOKENS = 2_000_000

SETTING_ENABLED = "paid_spend_cap_enabled"
SETTING_USD = "paid_spend_cap_daily_usd"
SETTING_TOKENS = "paid_spend_cap_daily_tokens"

_lock = threading.RLock()


class SpendCapReached(HTTPException):
    """The day's paid spend reached its cap; the call was refused before it ran."""

    def __init__(self, *, limit_kind: str, spent_usd: float, limit_usd: float,
                 spent_tokens: int, limit_tokens: int, resets_at: str):
        if limit_kind == "usd":
            what = f"${spent_usd:.4f} of the ${limit_usd:.2f} daily limit"
        else:
            what = f"{spent_tokens:,} of the {limit_tokens:,} daily token limit"
        message = (f"Daily paid-provider spend cap reached ({what}). Paid models are paused "
                   f"until {resets_at}; local models are unaffected. Raise or disable the cap "
                   f"in Settings > Agent (Daily paid spend cap).")
        super().__init__(status_code=429, detail=message)
        self.message = message
        self.error_class = ERROR_CLASS
        self.retryable = False
        self.limit_kind = limit_kind
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        self.spent_tokens = spent_tokens
        self.limit_tokens = limit_tokens
        self.resets_at = resets_at

    def as_dict(self) -> Dict[str, Any]:
        return {
            "error": self.message, "status": 429, "error_class": ERROR_CLASS,
            "retryable": False, "limit_kind": self.limit_kind,
            "spent_usd": round(self.spent_usd, 6), "limit_usd": self.limit_usd,
            "spent_tokens": self.spent_tokens, "limit_tokens": self.limit_tokens,
            "resets_at": self.resets_at,
        }


# --- settings ---------------------------------------------------------------

def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        value = get_setting(key, default)
        return default if value is None else value
    except Exception:  # noqa: BLE001
        return default


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number >= 0 else default


def limits() -> Dict[str, Any]:
    return {
        "enabled": bool(_setting(SETTING_ENABLED, DEFAULT_ENABLED)),
        "daily_usd": _number(_setting(SETTING_USD, DEFAULT_DAILY_USD), DEFAULT_DAILY_USD),
        "daily_tokens": int(_number(_setting(SETTING_TOKENS, DEFAULT_DAILY_TOKENS), DEFAULT_DAILY_TOKENS)),
    }


# --- classification ---------------------------------------------------------

def is_paid_endpoint(url: str) -> bool:
    """True for a remote endpoint that can bill money (everything not local)."""
    url = str(url or "").strip()
    if not url or url.startswith("faustus-cli://"):
        return False
    try:
        from src.model_context import is_local_endpoint
        return not is_local_endpoint(url)
    except Exception:  # noqa: BLE001
        return False


def _provider_label(url: str) -> str:
    try:
        from src.llm_core import _provider_label as label
        name = str(label(url) or "").strip()
        if name:
            return name
    except Exception:  # noqa: BLE001
        pass
    return urlparse(str(url or "")).hostname or "unknown"


# --- persistence ------------------------------------------------------------

def _now() -> datetime:
    return datetime.now().astimezone()


def _fresh(day: str) -> Dict[str, Any]:
    return {"day": day, "usd": 0.0, "tokens": 0, "calls": 0, "unknown_cost_calls": 0,
            "by_provider": {}, "notified_day": ""}


def _load(now: datetime) -> Dict[str, Any]:
    day = now.strftime("%Y-%m-%d")
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state, dict) and state.get("day") == day:
            base = _fresh(day)
            base.update({k: state[k] for k in base if k in state})
            if not isinstance(base["by_provider"], dict):
                base["by_provider"] = {}
            return base
    except (OSError, ValueError):
        pass
    return _fresh(day)


def _save(state: Dict[str, Any]) -> None:
    try:
        folder = os.path.dirname(STATE_FILE) or "."
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".spend_cap.", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, STATE_FILE)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        logger.debug("[spend_cap] could not persist state", exc_info=True)


def resets_at(now: Optional[datetime] = None) -> str:
    now = now or _now()
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.isoformat(timespec="seconds")


# --- accounting -------------------------------------------------------------

def _usage_tokens(usage: Dict[str, Any]) -> int:
    def count(key: str) -> int:
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return int(value) if math.isfinite(value) and value > 0 else 0
    split = count("input_tokens") + count("output_tokens")
    return split or count("total_tokens")


def record(url: str, usage: Optional[Dict[str, Any]], *, now: Optional[datetime] = None) -> None:
    """Book one paid call. ``usage`` may be empty: that is an unknown-cost call."""
    try:
        if not is_paid_endpoint(url):
            return
        usage = usage if isinstance(usage, dict) else {}
        tokens = _usage_tokens(usage)
        cost = usage.get("cost_usd")
        known = (not isinstance(cost, bool) and isinstance(cost, (int, float))
                 and math.isfinite(cost) and cost >= 0)
        label = _provider_label(url)
        with _lock:
            state = _load(now or _now())
            state["tokens"] = int(state["tokens"]) + tokens
            state["calls"] = int(state["calls"]) + 1
            row = state["by_provider"].setdefault(
                label, {"usd": 0.0, "tokens": 0, "calls": 0, "unknown_cost_calls": 0})
            row["tokens"] = int(row.get("tokens", 0)) + tokens
            row["calls"] = int(row.get("calls", 0)) + 1
            if known:
                state["usd"] = float(state["usd"]) + float(cost)
                row["usd"] = float(row.get("usd", 0.0)) + float(cost)
            else:
                state["unknown_cost_calls"] = int(state["unknown_cost_calls"]) + 1
                row["unknown_cost_calls"] = int(row.get("unknown_cost_calls", 0)) + 1
            _save(state)
    except Exception:  # noqa: BLE001 - bookkeeping never breaks a call
        logger.debug("[spend_cap] record failed", exc_info=True)


def _exceeded(state: Dict[str, Any], lim: Dict[str, Any]) -> Optional[str]:
    if lim["daily_usd"] > 0 and float(state["usd"]) >= lim["daily_usd"]:
        return "usd"
    if lim["daily_tokens"] > 0 and int(state["tokens"]) >= lim["daily_tokens"]:
        return "tokens"
    return None


def _notify_once(state: Dict[str, Any], error: SpendCapReached) -> None:
    if state.get("notified_day") == state["day"]:
        return
    state["notified_day"] = state["day"]
    _save(state)
    try:
        from src import notifications
        notifications.emit(
            "turn_error", owner=None, title="Daily paid spend cap reached",
            body=error.message[:200],
            data={"kind": "spend_cap", "limit_kind": error.limit_kind,
                  "spent_usd": round(error.spent_usd, 6), "spent_tokens": error.spent_tokens,
                  "resets_at": error.resets_at})
    except Exception:  # noqa: BLE001
        logger.debug("[spend_cap] notification failed", exc_info=True)


def gate(url: str, *, now: Optional[datetime] = None) -> None:
    """Refuse a paid call once today's cap is reached; no-op for local/disabled."""
    error: Optional[SpendCapReached] = None
    try:
        lim = limits()
        if not lim["enabled"] or not is_paid_endpoint(url):
            return
        now = now or _now()
        with _lock:
            state = _load(now)
            kind = _exceeded(state, lim)
            if kind is None:
                return
            error = SpendCapReached(
                limit_kind=kind, spent_usd=float(state["usd"]), limit_usd=lim["daily_usd"],
                spent_tokens=int(state["tokens"]), limit_tokens=lim["daily_tokens"],
                resets_at=resets_at(now))
            _notify_once(state, error)
    except Exception:  # noqa: BLE001
        logger.debug("[spend_cap] gate failed open", exc_info=True)
        return
    if error is not None:
        raise error


def status(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or _now()
    lim = limits()
    with _lock:
        state = _load(now)
    kind = _exceeded(state, lim) if lim["enabled"] else None
    providers = [
        {"provider": name, "usd": round(float(row.get("usd", 0.0)), 6),
         "tokens": int(row.get("tokens", 0)), "calls": int(row.get("calls", 0)),
         "unknown_cost_calls": int(row.get("unknown_cost_calls", 0))}
        for name, row in sorted(state["by_provider"].items())
    ]
    return {
        "enabled": lim["enabled"],
        "limit_usd": lim["daily_usd"],
        "limit_tokens": lim["daily_tokens"],
        "day": state["day"],
        "spent_usd": round(float(state["usd"]), 6),
        "tokens_today": int(state["tokens"]),
        "paid_calls": int(state["calls"]),
        "unknown_cost_calls": int(state["unknown_cost_calls"]),
        "reached": kind is not None,
        "reached_by": kind,
        "resets_at": resets_at(now),
        "by_provider": providers,
        "note": ("Calls without a reported cost count tokens only; dollars are never guessed. "
                 "Local models are never counted or limited."),
    }
