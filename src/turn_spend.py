"""turn_spend.py — one account for everything a turn spends on model calls.

Before this module, the main rounds, the compactor and the recovery ladder were
charged to the turn's budget ledger, and delegated workers were booked in the
run's SQLite account, but every other model call a turn makes (the advisor,
typed decisions, grounding retries, deep-research extraction, the workflow
agent node, memory consolidation, ...) spent tokens nobody added up.

The account is installed for the life of one agent turn. Every non-streaming
model call made inside it (``src.llm_core.llm_call_async``) is booked here, per
attempt, under a purpose label, in the same SQLite account the workers use
(``src.budget_account``, keyed by the run id):

* a call that reports usage adds its tokens; a retry that reports usage adds
  again rather than replacing the first attempt;
* a call whose provider reported nothing, or whose outcome is uncertain after a
  timeout, is recorded as unknown usage. Its cost is never a known zero and the
  run's cost reads ``"unknown"``;
* a local endpoint is a known cost of zero; a remote one without a price is an
  unpriced cost, not a free one;
* in ``enforce`` mode a call first reserves an estimate and is refused up front
  when the turn is already over budget; the reservation is released (or
  reconciled) exactly once, including when the call is cancelled;
* calls the loop already charges itself (compaction and recovery pass their own
  usage observer) are recorded here for the breakdown but not charged to the
  loop's ledger a second time.

``record`` (default) books and charges; ``enforce`` also refuses; ``off`` does
nothing. The account never raises into a model call for a bookkeeping failure.
"""

from __future__ import annotations

import contextvars
import logging
import sys
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_RECORD = "record"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_RECORD, MODE_ENFORCE)

_CURRENT: "contextvars.ContextVar[Optional[TurnAccount]]" = contextvars.ContextVar(
    "faustus_turn_spend_account", default=None)
_PURPOSE: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "faustus_turn_spend_purpose", default="")


class TurnSpendExceeded(RuntimeError):
    """The turn is over budget; an auxiliary call was refused before it ran."""

    def __init__(self, reason: str, *, purpose: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.purpose = purpose


def settings_mode() -> str:
    try:
        from src.settings import get_setting
        value = str(get_setting("agent_turn_spend", MODE_RECORD) or MODE_RECORD)
    except Exception:  # noqa: BLE001
        return MODE_RECORD
    return value if value in MODES else MODE_RECORD


@contextmanager
def spend_purpose(label: str):
    """Name the purpose of the model calls made inside the block."""
    token = _PURPOSE.set(str(label or ""))
    try:
        yield
    finally:
        try:
            _PURPOSE.reset(token)
        except ValueError:  # reset from another context: the value dies with it
            _PURPOSE.set("")


def current() -> "Optional[TurnAccount]":
    return _CURRENT.get()


def install(run_id: str, *, mode: Optional[str] = None) -> Optional[contextvars.Token]:
    """Open the turn's account; returns a token for ``uninstall`` (or None)."""
    mode = mode or settings_mode()
    if mode == MODE_OFF or not run_id:
        return None
    return _CURRENT.set(TurnAccount(str(run_id), mode))


def uninstall(token: Optional[contextvars.Token]) -> None:
    if token is None:
        return
    account = _CURRENT.get()
    try:
        _CURRENT.reset(token)
    except (ValueError, RuntimeError):
        _CURRENT.set(None)
    if account is not None:
        account.close()


def _caller_purpose() -> str:
    """Module of the first frame outside the model-call plumbing."""
    frame = sys._getframe(2)
    skip = ("src.llm_core", "src.turn_spend", "src.llm_trace", "contextlib", "asyncio")
    while frame is not None:
        name = str(frame.f_globals.get("__name__") or "")
        if not name.startswith(skip):
            return name.removeprefix("src.") or "auxiliary"
        frame = frame.f_back
    return "auxiliary"


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))
                                     or not value.is_integer()):
        return None
    count = int(value)
    return count if 0 <= count <= 2**63 - 1 else None


def tokens_from_usage(usage: Dict[str, Any]) -> Optional[int]:
    """Observed total, or the sum of the observed parts, or None when neither."""
    total = _count(usage.get("total_tokens"))
    parts = [_count(usage.get(k)) for k in ("input_tokens", "output_tokens")]
    summed = sum(n for n in parts if n is not None) if any(n is not None for n in parts) else None
    if total is None:
        return summed
    # A stale total must not under-report what the parts show.
    return max(total, summed or 0)


def _cost(usage: Dict[str, Any]) -> Optional[float]:
    value = usage.get("cost_usd")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value == value and 0 <= value < float("inf") else None


class CallSpend:
    """One model call: reserve, observe per attempt, settle exactly once."""

    def __init__(self, account: "TurnAccount", purpose: str, child_id: str,
                 *, observer_charged: bool, reserved: bool):
        self.account = account
        self.purpose = purpose
        self.child_id = child_id
        self.observer_charged = observer_charged
        self.reserved = reserved
        self._usages: List[Dict[str, Any]] = []
        self._local: Optional[bool] = None
        self._unknown_attempts = 0
        self._settled = False

    def observe(self, usage: Dict[str, Any], *, endpoint_local: bool = False) -> None:
        """One provider attempt that reported usage. Attempts add."""
        if self._settled or not isinstance(usage, dict):
            return
        self._usages.append(dict(usage))
        self._local = bool(endpoint_local)

    def note_unknown_attempt(self) -> None:
        """An attempt that may have completed upstream without telling us."""
        if not self._settled:
            self._unknown_attempts += 1

    def settle(self, outcome: str) -> Optional[Dict[str, Any]]:
        """Close the call. ``outcome`` is ``ok``, ``error`` or ``cancelled``.

        Idempotent: only the first call books or releases anything.
        """
        if self._settled:
            return None
        self._settled = True
        self.account._forget(self)
        tokens = 0
        observed = False
        costs: List[Optional[float]] = []
        for usage in self._usages:
            n = tokens_from_usage(usage)
            if n is not None:
                observed = True
                tokens += n
            costs.append(_cost(usage))
        attempts = max(1, len(self._usages) + self._unknown_attempts)
        unknown = False
        if not observed:
            # Nothing reported: an answer with no usage, an uncertain outcome
            # after a timeout, or a cancel while the call was in flight.
            unknown = (outcome == "ok" or outcome == "cancelled"
                       or self._unknown_attempts > 0)
        elif self._unknown_attempts:
            unknown = True  # some attempt is unaccounted for
        if not observed and not unknown:
            # A call that failed before anything was spent, as far as anyone
            # can tell: free the reservation, record nothing.
            if self.reserved:
                self.account._release(self.child_id)
            return {"released": True}
        if self._local:
            cost: Optional[float] = 0.0
        elif costs and all(c is not None for c in costs):
            cost = sum(c for c in costs if c is not None)
        else:
            cost = None
        record = self.account._reconcile(
            self.child_id, tokens, cost, purpose=self.purpose,
            usage_unknown=unknown, attempts=attempts)
        if observed and not self.observer_charged:
            self.account._charge(self.purpose, self._usages, local=bool(self._local))
        return record


class TurnAccount:
    def __init__(self, run_id: str, mode: str):
        self.run_id = run_id
        self.mode = mode
        self._charge_cb: Optional[Callable[..., None]] = None
        self._admit_cb: Optional[Callable[[str], Optional[str]]] = None
        self._open: Dict[str, CallSpend] = {}
        self._opened_run = False

    # -- wiring by the loop -------------------------------------------------

    def bind(self, *, charge: Optional[Callable[..., None]] = None,
             admit: Optional[Callable[[str], Optional[str]]] = None) -> None:
        """Attach the loop's ledger: ``charge(usage, purpose=, endpoint_local=)``
        and ``admit(url) -> reason or None``."""
        self._charge_cb = charge
        self._admit_cb = admit

    # -- store helpers (never raise into a model call) -----------------------

    def _ensure_run(self) -> None:
        if self._opened_run:
            return
        try:
            from src import budget_account
            budget_account.open(self.run_id, ceiling_tokens=_run_ceiling())
            self._opened_run = True
        except Exception:  # noqa: BLE001
            logger.debug("turn_spend: could not open account", exc_info=True)

    def _reconcile(self, child_id: str, tokens: int, cost: Optional[float], **kw) -> Optional[Dict[str, Any]]:
        try:
            from src import budget_account
            self._ensure_run()
            return budget_account.reconcile(self.run_id, child_id, tokens, cost, **kw)
        except Exception:  # noqa: BLE001
            logger.warning("turn_spend: could not book a call", exc_info=True)
            return None

    def _release(self, child_id: str) -> bool:
        try:
            from src import budget_account
            return budget_account.release(self.run_id, child_id)
        except Exception:  # noqa: BLE001
            logger.warning("turn_spend: could not release a reservation", exc_info=True)
            return False

    def _charge(self, purpose: str, usages: List[Dict[str, Any]], *, local: bool) -> None:
        if self._charge_cb is None:
            return
        for usage in usages:
            try:
                self._charge_cb(usage, purpose=purpose, endpoint_local=local)
            except Exception:  # noqa: BLE001
                logger.debug("turn_spend: ledger charge failed", exc_info=True)

    def _forget(self, call: CallSpend) -> None:
        self._open.pop(call.child_id, None)

    # -- the two entry points --------------------------------------------------

    def begin(self, *, url: str, estimate_tokens: int, observer_charged: bool,
              purpose: Optional[str] = None) -> CallSpend:
        """Start accounting for one call; in enforce mode, admit or refuse it."""
        label = (purpose or _PURPOSE.get() or _caller_purpose()) or "auxiliary"
        child_id = f"{label}#{uuid.uuid4().hex[:8]}"
        reserved = False
        if self.mode == MODE_ENFORCE:
            reason = None
            if self._admit_cb is not None:
                try:
                    reason = self._admit_cb(url)
                except Exception:  # noqa: BLE001 - a broken check does not refuse work
                    logger.debug("turn_spend: admission check failed", exc_info=True)
            if reason:
                raise TurnSpendExceeded(str(reason), purpose=label)
            try:
                from src import budget_account
                self._ensure_run()
                outcome = budget_account.reserve(
                    self.run_id, child_id, max(0, int(estimate_tokens)), purpose=label)
                if isinstance(outcome, budget_account.BudgetExceeded):
                    raise TurnSpendExceeded(outcome.reason, purpose=label)
                reserved = True
            except TurnSpendExceeded:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("turn_spend: could not reserve", exc_info=True)
        call = CallSpend(self, label, child_id, observer_charged=observer_charged,
                         reserved=reserved)
        self._open[child_id] = call
        return call

    def record_main(self, *, round_num: int, input_tokens: int, output_tokens: int,
                    cost_usd: Optional[float], endpoint_local: bool) -> None:
        """Mirror a main-stream round into the account for the breakdown.

        The loop's own ledger already charges it; this only books it.
        """
        tokens = (input_tokens or 0) + (output_tokens or 0)
        cost = 0.0 if endpoint_local else (float(cost_usd) if cost_usd is not None else None)
        self._reconcile(f"main#{round_num}", tokens, cost, purpose="main")

    def close(self) -> None:
        """End of turn: settle whatever is still open, once each."""
        for call in list(self._open.values()):
            call.settle("cancelled")


def _run_ceiling() -> int:
    try:
        from src.settings import get_setting
        return int(get_setting("agent_budget_tokens_per_run", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def estimate_call_tokens(messages: Any, max_tokens: Any) -> int:
    """Rough reservation size: prompt characters over three plus the output cap."""
    chars = 0
    try:
        for message in messages or ():
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                chars += len(content)
            elif isinstance(content, list):
                chars += sum(len(p.get("text", "")) for p in content
                             if isinstance(p, dict) and isinstance(p.get("text"), str))
    except Exception:  # noqa: BLE001
        pass
    out = _count(max_tokens) or 0
    return chars // 3 + out


def explain(run_id: str) -> Dict[str, Any]:
    """Where a run's spend went: per purpose, with unknowns kept unknown."""
    from src import budget_account
    snap = budget_account.snapshot(run_id)
    rows = sorted(
        ({"purpose": name, **entry} for name, entry in snap.get("purposes", {}).items()),
        key=lambda r: (-int(r["consumed_tokens"]), r["purpose"]))
    return {
        "run_id": run_id,
        "consumed_tokens": snap["consumed_tokens"],
        "consumed_cost": snap["consumed_cost"],
        "unknown_usage_calls": snap.get("unknown_usage_calls", 0),
        "reserved_tokens": snap["reserved_tokens"],
        "purposes": rows,
    }
