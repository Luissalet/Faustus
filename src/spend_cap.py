"""spend_cap.py - a default-on DAILY hard cap on spend with PAID remote providers.

Why: an unattended loop, a runaway sub-agent fan-out or a long research turn can
spend real money on a paid endpoint while nobody watches. The per-turn ceilings
(src/autonomy_budget.py, agent_turn_max_cost_usd) bound one turn; this bounds
the DAY, across every turn, task, sub-agent, advisor and utility call.

Design
------
* One accumulator, persisted to DATA_DIR/spend_cap.json and keyed by the LOCAL
  calendar date. A new date starts from zero (the old day is simply dropped).
  Every load-modify-save runs under a real mutual exclusion shared by all the
  processes that use the same DATA_DIR (a lock file: msvcrt.locking on
  Windows, fcntl.flock elsewhere, plus a thread lock inside one process), so
  two workers can never overwrite each other's booking.
* One choke point: ``src/llm_core.py`` calls :func:`gate` immediately before
  EVERY billable HTTP attempt - the first send, each retry and each fallback
  candidate of ``llm_call``, ``llm_call_async`` and ``stream_llm`` - and
  :func:`record` with the provider-reported usage as soon as a response (even a
  failed one that carries a usage receipt) is in. A call that was already
  accepted is never cut mid-stream; only the NEXT attempt is refused.
* A response-cache hit, a ``faustus-cli://`` client and a local endpoint never
  reach :func:`gate`/:func:`record` as paid calls: they are not new paid
  requests, so they are neither refused nor counted.

What counts as PAID (see :func:`classify`)
------------------------------------------
Paid means "the provider/model is actually billed per call", not "the URL is
remote". In order:

1. empty URL or ``faustus-cli://`` (a CLI the user already pays a flat fee
   for)                                                       -> not paid
2. a local endpoint: the configured ``endpoint_kind`` is ``local`` (llama
   server, Ollama, Sparks...), or the address is loopback / RFC1918 /
   tailscale and the endpoint is not declared ``api``/``proxy`` -> not paid
3. a flat-rate subscription provider (ChatGPT subscription, GitHub Copilot)
                                                              -> not paid
4. a model the provider publishes as free (id ending in ``:free``, the
   OpenRouter convention)                                     -> not paid
5. anything else that is remote (``endpoint_kind`` api/proxy or a public
   host)                                                      -> PAID

Pricing: Faustus has no price table, so the money measured is the provider's
own reported ``cost_usd``. A paid call whose response reports a cost counts
that cost in dollars. A response that reports ``cost_usd: 0`` is, by the
provider's own statement, free: it adds $0 and its tokens do NOT feed the token
cap (so a free-in-practice provider is never blocked by a limit it cannot
spend). A paid call that reports no cost has an UNKNOWN price: it counts its
tokens (the token cap is the protection for exactly this case) and is tallied
in ``unknown_cost_calls``; dollars are never guessed. If the dollar limit is
0 (off), the token cap covers every paid token instead.

When the day's total has reached a limit, :func:`gate` raises
:class:`SpendCapReached` (HTTP 429, ``error_class = "spend_cap.reached"``)
before the request goes out, and sends ONE notification for that day.

Settings: ``paid_spend_cap_enabled`` (default true), ``paid_spend_cap_daily_usd``
(default 5.0; 0 = no dollar cap), ``paid_spend_cap_daily_tokens`` (default
2,000,000; 0 = no token cap). They can only be changed by a person (see
``routes/spend_cap_routes.py``, the settings API and ``manage_settings``).

Every bookkeeping failure is swallowed: this module never breaks a model call
except by the deliberate refusal.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import math
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, Optional
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
#: The settings only a person may change (never an agent channel).
PROTECTED_SETTINGS = frozenset({SETTING_ENABLED, SETTING_USD, SETTING_TOKENS})

#: Flat-rate subscription providers: a call is not billed per request.
_FLAT_RATE_PROVIDERS = frozenset({"chatgpt-subscription", "copilot"})

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

def classify(url: str, model: str = "") -> str:
    """``"paid"`` or the reason the call is not billed (``"cli"``, ``"local"``,
    ``"subscription"``, ``"free-model"``, ``"none"``). See the module docstring."""
    url = str(url or "").strip()
    if not url:
        return "none"
    if url.startswith("faustus-cli://"):
        return "cli"
    try:
        from src.model_context import is_local_endpoint
        if is_local_endpoint(url):
            return "local"
    except Exception:  # noqa: BLE001 - unknown endpoint: do not claim it is billed
        return "local"
    try:
        from src.llm_core import _detect_provider
        if _detect_provider(url) in _FLAT_RATE_PROVIDERS:
            return "subscription"
    except Exception:  # noqa: BLE001
        pass
    if str(model or "").strip().lower().endswith(":free"):
        return "free-model"
    return "paid"


def is_paid_endpoint(url: str, model: str = "") -> bool:
    """True only for a call that the provider actually bills (see :func:`classify`)."""
    return classify(url, model) == "paid"


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
    return {"day": day, "usd": 0.0, "tokens": 0, "tokens_unpriced": 0, "calls": 0,
            "unknown_cost_calls": 0, "by_provider": {}, "notified_day": ""}


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
            if "tokens_unpriced" not in state:
                # state written before the split: every token was counted
                base["tokens_unpriced"] = int(base["tokens"])
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


def _lock_path() -> str:
    return STATE_FILE + ".lock"


def _os_lock(fh) -> None:
    """Block until this process holds the exclusive lock on ``fh``."""
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        deadline = time.monotonic() + 30.0
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.005)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _os_unlock(fh) -> None:
    if os.name == "nt":
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _exclusive() -> Iterator[None]:
    """Mutual exclusion for load-modify-save across threads AND processes."""
    with _lock:
        try:
            os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
            fh = open(_lock_path(), "a+b")
        except OSError:
            # cannot create the lock file (read-only data dir): the save fails too
            yield
            return
        try:
            _os_lock(fh)
            try:
                yield
            finally:
                try:
                    _os_unlock(fh)
                except OSError:
                    pass
        finally:
            fh.close()


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


def _known_cost(usage: Dict[str, Any]) -> Optional[float]:
    cost = usage.get("cost_usd")
    if (not isinstance(cost, bool) and isinstance(cost, (int, float))
            and math.isfinite(cost) and cost >= 0):
        return float(cost)
    return None


def record(url: str, usage: Optional[Dict[str, Any]], *, model: str = "",
           now: Optional[datetime] = None) -> None:
    """Book one paid call. ``usage`` may be empty: that is an unknown-cost call."""
    try:
        if not is_paid_endpoint(url, model):
            return
        usage = usage if isinstance(usage, dict) else {}
        tokens = _usage_tokens(usage)
        cost = _known_cost(usage)
        label = _provider_label(url)
        with _exclusive():
            state = _load(now or _now())
            state["tokens"] = int(state["tokens"]) + tokens
            state["calls"] = int(state["calls"]) + 1
            row = state["by_provider"].setdefault(
                label, {"usd": 0.0, "tokens": 0, "calls": 0, "unknown_cost_calls": 0})
            row["tokens"] = int(row.get("tokens", 0)) + tokens
            row["calls"] = int(row.get("calls", 0)) + 1
            if cost is not None:
                state["usd"] = float(state["usd"]) + cost
                row["usd"] = float(row.get("usd", 0.0)) + cost
            else:
                state["tokens_unpriced"] = int(state["tokens_unpriced"]) + tokens
                state["unknown_cost_calls"] = int(state["unknown_cost_calls"]) + 1
                row["unknown_cost_calls"] = int(row.get("unknown_cost_calls", 0)) + 1
            _save(state)
    except Exception:  # noqa: BLE001 - bookkeeping never breaks a call
        logger.debug("[spend_cap] record failed", exc_info=True)


def has_receipt(usage: Optional[Dict[str, Any]]) -> bool:
    """True when ``usage`` carries tokens or a cost the provider reported."""
    return isinstance(usage, dict) and (_usage_tokens(usage) > 0 or _known_cost(usage) is not None)


def _token_basis(state: Dict[str, Any], lim: Dict[str, Any]) -> int:
    """Tokens the token cap judges: the ones whose price is unknown. With the
    dollar limit off, every paid token (nothing else would bound the day)."""
    if lim["daily_usd"] > 0:
        return int(state["tokens_unpriced"])
    return int(state["tokens"])


def _exceeded(state: Dict[str, Any], lim: Dict[str, Any]) -> Optional[str]:
    if lim["daily_usd"] > 0 and float(state["usd"]) >= lim["daily_usd"]:
        return "usd"
    if lim["daily_tokens"] > 0 and _token_basis(state, lim) >= lim["daily_tokens"]:
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


def gate(url: str, model: str = "", *, now: Optional[datetime] = None) -> None:
    """Refuse a paid HTTP attempt once today's cap is reached; no-op otherwise.

    Call it immediately before each attempt that can bill (not before a cache
    hit, a CLI client or a local endpoint)."""
    error: Optional[SpendCapReached] = None
    try:
        lim = limits()
        if not lim["enabled"] or not is_paid_endpoint(url, model):
            return
        now = now or _now()
        with _exclusive():
            state = _load(now)
            kind = _exceeded(state, lim)
            if kind is None:
                return
            error = SpendCapReached(
                limit_kind=kind, spent_usd=float(state["usd"]), limit_usd=lim["daily_usd"],
                spent_tokens=_token_basis(state, lim) if kind == "tokens" else int(state["tokens"]),
                limit_tokens=lim["daily_tokens"], resets_at=resets_at(now))
            _notify_once(state, error)
    except Exception:  # noqa: BLE001
        logger.debug("[spend_cap] gate failed open", exc_info=True)
        return
    if error is not None:
        raise error


# --- streams: one ledger per stream_llm call --------------------------------

class AttemptLedger:
    """Books a stream's usage per HTTP attempt, without double counting.

    Inside one attempt every ``usage`` frame is a cumulative/partial update of
    the SAME response (a repeated final frame, an input-only frame followed by
    an output frame): later values overwrite earlier ones and nothing is
    summed. A new attempt (a retry or a fallback re-send, announced by
    :func:`attempt_gate`) is a genuinely new request and is booked on its own.
    An attempt that produced no usage is booked as an unknown-cost call only
    when it ended the stream successfully."""

    def __init__(self, url: str, model: str):
        self.url, self.model = url, model
        self._current: Optional[Dict[str, Any]] = None

    def settle_previous(self) -> None:
        """Book the receipt of the attempt that just ended (if it has one)."""
        try:
            self._flush(final=False, errored=True)
        except Exception:  # noqa: BLE001
            logger.debug("[spend_cap] settling failed", exc_info=True)

    def begin_attempt(self) -> None:
        self.settle_previous()
        self._current = {}

    def frame(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        if self._current is None:
            self._current = {}
        for key in ("input_tokens", "output_tokens", "total_tokens", "cost_usd"):
            value = usage.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if math.isfinite(value) and value >= 0:
                self._current[key] = value

    def _flush(self, *, final: bool, errored: bool) -> None:
        current, self._current = self._current, None
        if current is None:
            return
        if has_receipt(current):
            record(self.url, current, model=self.model)
        elif final and not errored:
            record(self.url, {}, model=self.model)

    def finish(self, *, errored: bool) -> None:
        try:
            self._flush(final=True, errored=errored)
        except Exception:  # noqa: BLE001
            logger.debug("[spend_cap] stream ledger failed", exc_info=True)


_ledger: contextvars.ContextVar[Optional[AttemptLedger]] = contextvars.ContextVar(
    "spend_cap_ledger", default=None)


def use_ledger(ledger: Optional[AttemptLedger]) -> None:
    _ledger.set(ledger)


def attempt_gate(url: str, model: str = "") -> None:
    """:func:`gate` for one stream attempt, then mark the attempt boundary.

    The previous attempt is settled FIRST: whatever receipt it delivered is
    booked before the gate decides, so a retry after an accepted (and already
    reported) cost sees it. A refused attempt was never sent and books nothing."""
    ledger = _ledger.get()
    if ledger is not None:
        ledger.settle_previous()
    gate(url, model)
    if ledger is not None:
        ledger.begin_attempt()


def current_ledger() -> Optional[AttemptLedger]:
    return _ledger.get()


def status(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or _now()
    lim = limits()
    with _exclusive():
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
        "tokens_counted_for_cap": _token_basis(state, lim),
        "paid_calls": int(state["calls"]),
        "unknown_cost_calls": int(state["unknown_cost_calls"]),
        "reached": kind is not None,
        "reached_by": kind,
        "resets_at": resets_at(now),
        "by_provider": providers,
        "note": ("Only providers that bill per call count (not local, CLI, subscription or "
                 ":free models). Dollars are the provider's reported cost, never guessed; a "
                 "paid call without a reported cost counts tokens against the token cap, and "
                 "a call that reports cost 0 is free and not token-capped."),
    }
