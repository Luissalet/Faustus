"""decision_forks.py -- typed decisions at three forks of the agent loop.

``src/typed_decision.py`` answers a closed question from the next-token
probabilities of the allowed answers: one prefill, a confidence and a
``mass``, and "unknown" whenever either is low. This module puts that to work
at three places where the loop used to pick by habit, each behind its own
setting (all off by default) and each writing a *receipt* -- options, choice,
confidence, mass, whether the old behaviour was kept, and what came of it --
into the turn's trace (``metrics["decision_receipts"]``) and into
process-wide counters (``stats()``).

* ``tool_error``       after a tool call failed: retry the same call, change
  its arguments, use another tool, or stop. Above the confidence threshold the
  answer is injected as a runtime hint; the working model still acts. Below
  it nothing new happens.
* ``tool_tie``         when the top three lexical scores of the tool index tie
  (within a relative tolerance), which of the tied tools to promote first.
* ``compaction_keep``  in extractive compaction, for each old tool result:
  keep it verbatim or fold it into the digest. The last messages stay pinned.

A decision here is ADVISORY, like everywhere else: it never approves an
action, never widens a permission and never replaces a rule that is already
confident. Every entry point is total -- any failure keeps the old behaviour.
"""
from __future__ import annotations

import contextvars
import logging
import re
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

FORK_TOOL_ERROR = "tool_error"
FORK_TOOL_TIE = "tool_tie"
FORK_COMPACTION = "compaction_keep"
FORKS = (FORK_TOOL_ERROR, FORK_TOOL_TIE, FORK_COMPACTION)

DEFAULTS: Dict[str, Any] = {
    "typed_decision_error_fork": False,
    "typed_decision_tool_tie": False,
    "typed_decision_tool_tie_tolerance": 0.03,
    "typed_decision_compaction_keep": False,
}
_SETTING_FOR_FORK = {
    FORK_TOOL_ERROR: "typed_decision_error_fork",
    FORK_TOOL_TIE: "typed_decision_tool_tie",
    FORK_COMPACTION: "typed_decision_compaction_keep",
}

#: The error fork is asked at most this many times in one turn.
MAX_ERROR_FORKS_PER_TURN = 4
#: Messages at the end of the conversation extractive compaction never folds
#: when the compaction-keep fork is on.
PINNED_TAIL_MESSAGES = 6
#: Old tool results the compaction fork is asked about (the most recent ones
#: that the digest would actually cut), and how much of one it may keep.
MAX_COMPACTION_CANDIDATES = 8
VERBATIM_CHARS_PER_RESULT = 1500
VERBATIM_CHARS_TOTAL = 4000
_DIGEST_TOOL_CHARS = 140


def _setting(key: str) -> Any:
    default = DEFAULTS.get(key)
    try:
        from src.settings import get_setting
        value = get_setting(key, default)
    except Exception:  # noqa: BLE001 - unreadable settings = defaults
        return default
    return default if value is None else value


def fork_enabled(fork: str) -> bool:
    """The fork's own switch AND typed decisions as a whole."""
    key = _SETTING_FOR_FORK.get(fork)
    if not key or not bool(_setting(key)):
        return False
    try:
        from src import typed_decision
        return typed_decision.enabled()
    except Exception:  # noqa: BLE001
        return False


def tie_tolerance() -> float:
    try:
        return max(0.0, min(0.5, float(_setting("typed_decision_tool_tie_tolerance"))))
    except (TypeError, ValueError):
        return float(DEFAULTS["typed_decision_tool_tie_tolerance"])


# ---------------------------------------------------------------------------
# Receipts, the turn trace and the counters
# ---------------------------------------------------------------------------

def make_receipt(fork: str, options: Sequence[str], decision: Any, *, fallback: bool,
                 outcome: str, round_num: Optional[int] = None,
                 extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One receipt. ``decision`` is a ``typed_decision.Decision`` (or None when
    the question could not be asked at all)."""
    receipt: Dict[str, Any] = {
        "fork": fork,
        "options": [str(o) for o in options],
        "choice": getattr(decision, "value", None),
        "best": getattr(decision, "best", None),
        "confidence": getattr(decision, "confidence", None),
        "mass": getattr(decision, "mass", None),
        "method": getattr(decision, "method", "unavailable") if decision is not None else "unavailable",
        "reason": getattr(decision, "reason", "") if decision is not None else "not_asked",
        "ms": round(float(getattr(decision, "ms", 0.0) or 0.0), 1) if decision is not None else 0.0,
        "fallback": bool(fallback),
        "outcome": outcome,
    }
    if round_num is not None:
        receipt["round"] = round_num
    if extra:
        receipt.update(extra)
    return receipt


class DecisionTrace:
    """The receipts of one turn."""

    def __init__(self) -> None:
        self.receipts: List[Dict[str, Any]] = []
        self.error_forks = 0

    def add(self, receipt: Dict[str, Any]) -> Dict[str, Any]:
        self.receipts.append(receipt)
        return receipt


_TRACE: contextvars.ContextVar[Optional[DecisionTrace]] = contextvars.ContextVar(
    "faustus_decision_trace", default=None)


def begin_turn() -> Tuple[DecisionTrace, Any]:
    """Start a trace for this turn. Returns ``(trace, restore)``; calling
    ``restore()`` puts back whatever trace (an enclosing turn's) was current."""
    previous = _TRACE.get()
    trace = DecisionTrace()
    _TRACE.set(trace)

    def restore() -> None:
        try:
            _TRACE.set(previous)
        except Exception:  # noqa: BLE001
            pass
    return trace, restore


def current_trace() -> Optional[DecisionTrace]:
    return _TRACE.get()


def adopt(trace: Optional[DecisionTrace]) -> None:
    """Make ``trace`` the current one in THIS context. The agent loop is an
    async generator whose steps may run in different tasks (each with its own
    copy of the context), so it re-adopts its trace right before it runs
    tools, where the tie fork records."""
    try:
        _TRACE.set(trace)
    except Exception:  # noqa: BLE001
        pass


class _Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        lock = getattr(self, "_lock", None) or threading.Lock()
        with lock:
            self._lock = lock
            self.by_fork: Dict[str, Dict[str, Any]] = {}
            self.recent: Deque[Dict[str, Any]] = deque(maxlen=30)

    def note(self, receipt: Dict[str, Any]) -> None:
        with self._lock:
            row = self.by_fork.setdefault(str(receipt.get("fork")), {
                "asked": 0, "decided": 0, "fallback": 0, "choices": {}, "outcomes": {},
                "confidence_sum": 0.0, "confidence_n": 0, "ms_sum": 0.0,
            })
            row["asked"] += 1
            row["ms_sum"] += float(receipt.get("ms") or 0.0)
            if receipt.get("choice") is not None:
                row["decided"] += 1
                c = str(receipt["choice"])
                row["choices"][c] = row["choices"].get(c, 0) + 1
            if receipt.get("fallback"):
                row["fallback"] += 1
            if isinstance(receipt.get("confidence"), (int, float)):
                row["confidence_sum"] += float(receipt["confidence"])
                row["confidence_n"] += 1
            o = str(receipt.get("outcome") or "")
            row["outcomes"][o] = row["outcomes"].get(o, 0) + 1
            self.recent.append(dict(receipt))

    def outcome_changed(self, fork: str, old: str, new: str) -> None:
        with self._lock:
            row = self.by_fork.get(fork)
            if not row:
                return
            if row["outcomes"].get(old):
                row["outcomes"][old] -= 1
                if not row["outcomes"][old]:
                    del row["outcomes"][old]
            row["outcomes"][new] = row["outcomes"].get(new, 0) + 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            forks: Dict[str, Any] = {}
            for name, row in self.by_fork.items():
                asked = row["asked"]
                forks[name] = {
                    "asked": asked, "decided": row["decided"], "fallback": row["fallback"],
                    "choices": dict(row["choices"]), "outcomes": dict(row["outcomes"]),
                    "mean_confidence": (round(row["confidence_sum"] / row["confidence_n"], 4)
                                        if row["confidence_n"] else None),
                    "mean_ms": round(row["ms_sum"] / asked, 1) if asked else None,
                }
            return {"forks": forks, "recent": list(self.recent)}


_STATS = _Stats()


def record(receipt: Dict[str, Any], *, trace: Optional[DecisionTrace] = None) -> Dict[str, Any]:
    """Count a receipt and append it to the turn's trace (the given one, else
    the current one)."""
    _STATS.note(receipt)
    target = trace if trace is not None else current_trace()
    if target is not None:
        target.add(receipt)
    return receipt


def update_outcome(receipt: Dict[str, Any], outcome: str) -> None:
    old = str(receipt.get("outcome") or "")
    if old == outcome:
        return
    receipt["outcome"] = outcome
    _STATS.outcome_changed(str(receipt.get("fork")), old, outcome)


def stats() -> Dict[str, Any]:
    out = _STATS.snapshot()
    out["enabled"] = {fork: bool(_setting(key)) for fork, key in _SETTING_FOR_FORK.items()}
    out["tie_tolerance"] = tie_tolerance()
    return out


def _reset_stats() -> None:
    _STATS.reset()


# ---------------------------------------------------------------------------
# Fork 1: after a tool error
# ---------------------------------------------------------------------------

ERROR_OPTIONS: Tuple[str, ...] = ("retry_same", "change_arguments", "other_tool", "stop")
_ERROR_DESCRIPTIONS = {
    "retry_same": "the failure looks temporary (timeout, busy, network); the same call may work again",
    "change_arguments": "the tool is right but its arguments are wrong (path, value, syntax, missing field)",
    "other_tool": "this tool cannot do the job; another tool or another approach is needed",
    "stop": "the failure cannot be fixed from here (permission, missing resource, blocked); stop and tell the user",
}
_ERROR_SKIP_TOOLS = frozenset({"ask_user", "update_plan", "todowrite", "lookup_tools"})


def is_failed_result(result: Any) -> bool:
    """True for a tool result that is a genuine failure (not a policy block,
    not an approval card, not a question to the user)."""
    if not isinstance(result, dict):
        return False
    if result.get("blocked") or result.get("approval_required") or result.get("ask_user"):
        return False
    if result.get("error"):
        return True
    code = result.get("exit_code")
    return isinstance(code, int) and not isinstance(code, bool) and code != 0


def failure_text(result: Dict[str, Any], limit: int = 500) -> str:
    text = str(result.get("error") or "") or str(result.get("output") or result.get("results") or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def skips_error_fork(tool: str) -> bool:
    return str(tool) in _ERROR_SKIP_TOOLS


def _clip(text: Any, limit: int) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def error_hint(choice: str, tool: str) -> str:
    """The runtime hint for an answer. Advisory: the model decides."""
    head = ("[Runtime hint -- advisory, not a user request] A quick classifier read the failure "
            f"of `{tool}` and judged: ")
    tail = {
        "retry_same": "it looks temporary, so one more identical attempt is reasonable.",
        "change_arguments": "the tool is the right one but the arguments are not; fix them "
                            "(path, value, syntax) instead of repeating the same call.",
        "other_tool": "this tool is not suited to the job; use a different tool or approach.",
        "stop": "it cannot be fixed from here; stop retrying and tell the user plainly what "
                "failed and what is needed.",
    }.get(choice, "")
    return head + tail if tail else ""


async def decide_after_error(*, user_request: str, tool: str, args: str, error: str,
                             owner: Optional[str] = None, round_num: Optional[int] = None,
                             trace: Optional[DecisionTrace] = None) -> Tuple[Optional[str], Dict[str, Any]]:
    """Ask the error fork. Returns ``(hint or None, receipt)``. The receipt is
    recorded; ``hint`` is set only when the answer cleared the confidence and
    mass thresholds."""
    from src import typed_decision
    context = (
        f"User request: {_clip(user_request, 500)}\n"
        f"Tool called: {tool}\nArguments: {_clip(args, 400)}\n"
        f"Error returned: {_clip(error, 500)}"
    )
    fld = typed_decision.Field(
        name="next_step",
        question="The tool call above failed. What is the best next step for the agent?",
        choices=list(ERROR_OPTIONS),
        descriptions=[_ERROR_DESCRIPTIONS[o] for o in ERROR_OPTIONS],
    )
    decisions = await typed_decision.decide(context, [fld], owner=owner, caller="fork_tool_error")
    decision = decisions.get("next_step")
    known = decision is not None and decision.value is not None
    receipt = make_receipt(
        FORK_TOOL_ERROR, ERROR_OPTIONS, decision, fallback=not known,
        outcome="hint_injected" if known else "below_threshold", round_num=round_num,
        extra={"tool": tool},
    )
    record(receipt, trace=trace)
    return (error_hint(decision.value, tool) if known else None), receipt


def followed_choice(choice: str, failed_tool: str, failed_args: str,
                    next_tool: str, next_args: str) -> bool:
    """Whether the call that came after the hint did what the hint said."""
    same_tool = failed_tool == next_tool
    same_args = _norm(failed_args) == _norm(next_args)
    if choice == "retry_same":
        return same_tool and same_args
    if choice == "change_arguments":
        return same_tool and not same_args
    if choice == "other_tool":
        return not same_tool
    return False


def _norm(args: Any) -> str:
    try:
        from src.loop_breaker import normalize_args
        return normalize_args(args)
    except Exception:  # noqa: BLE001
        return str(args or "").strip()


# ---------------------------------------------------------------------------
# Fork 2: a tie between the top tools of the index
# ---------------------------------------------------------------------------

def tie_group(scored: Sequence[Tuple[str, float]], *, top: int = 3,
              tolerance: Optional[float] = None) -> List[str]:
    """The names among the first ``top`` whose score is within ``tolerance``
    (relative to the best one) of the best score. A group of two or more is a
    tie; fewer means the order was not arbitrary. A best score of zero never
    ties -- there is nothing to choose between."""
    tol = tie_tolerance() if tolerance is None else float(tolerance)
    rows = [(str(n), float(s)) for n, s in list(scored)[:max(1, top)]]
    if len(rows) < 2 or rows[0][1] <= 0:
        return []
    best = rows[0][1]
    group = [n for n, s in rows if best - s <= tol * best]
    return group if len(group) >= 2 else []


def choose_first_sync(query: str, tied: Sequence[str], descriptions: Dict[str, str], *,
                      owner: Optional[str] = None) -> Optional[str]:
    """Which of the tied tools to promote first. None when undecided (the
    caller keeps its order). Records a receipt either way."""
    from src import typed_decision
    names = [str(n) for n in tied][:typed_decision.MAX_CHOICES]
    if len(names) < 2:
        return None
    fld = typed_decision.Field(
        name="promote_first",
        question="Which tool should the agent be offered first for this request?",
        choices=names,
        descriptions=[_clip(descriptions.get(n, ""), 160) for n in names],
    )
    decisions = typed_decision.decide_sync(
        f"Request: {_clip(query, 600)}", [fld], owner=owner, caller="fork_tool_tie")
    decision = decisions.get("promote_first")
    known = decision is not None and decision.value in names
    record(make_receipt(
        FORK_TOOL_TIE, names, decision, fallback=not known,
        outcome="promoted_first" if known else "kept_order",
    ))
    return decision.value if known else None


def apply_tie_choice(ordered: List[str], tied: Sequence[str], chosen: Optional[str]) -> List[str]:
    """Move ``chosen`` to the earliest position any tied tool holds. Everything
    else keeps its relative order."""
    if not chosen or chosen not in ordered:
        return list(ordered)
    positions = [ordered.index(n) for n in tied if n in ordered]
    if not positions:
        return list(ordered)
    target = min(positions)
    out = [n for n in ordered if n != chosen]
    out.insert(min(target, len(out)), chosen)
    return out


# ---------------------------------------------------------------------------
# Fork 3: which old tool results extractive compaction keeps verbatim
# ---------------------------------------------------------------------------

def compaction_candidates(rows: Sequence[Dict[str, Any]], text_of) -> List[int]:
    """Indexes of the old tool results the digest would cut (longer than its
    per-row budget), newest first, at most ``MAX_COMPACTION_CANDIDATES``."""
    found: List[int] = []
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        if isinstance(row, dict) and row.get("role") == "tool":
            if len(re.sub(r"\s+", " ", text_of(row.get("content"))).strip()) > _DIGEST_TOOL_CHARS:
                found.append(index)
                if len(found) >= MAX_COMPACTION_CANDIDATES:
                    break
    return found


async def choose_verbatim(rows: Sequence[Dict[str, Any]], text_of, *, goal: str,
                          owner: Optional[str] = None) -> Set[int]:
    """Indexes (into ``rows``) of the old tool results to keep verbatim.
    Anything undecided is folded into the digest, as it always was."""
    from src import typed_decision
    candidates = compaction_candidates(rows, text_of)
    if not candidates:
        return set()
    fields = []
    names: Dict[str, int] = {}
    for index in candidates:
        row = rows[index]
        name = f"keep_{index}"
        names[name] = index
        label = str(row.get("name") or "tool")
        fields.append(typed_decision.Field(
            name=name,
            question=(f"Old tool result #{index} ({label}): "
                      f"\"{_clip(text_of(row.get('content')), 260)}\" -- will the agent still need "
                      "its exact text later to finish the task?"),
            choices="bool",
        ))
    started = time.monotonic()
    decisions = await typed_decision.decide(
        f"Task the agent is working on: {_clip(goal, 700)}", fields, owner=owner,
        timeout_s=min(8.0, 1.5 + 0.35 * len(fields)), caller="fork_compaction",
    )
    kept: Set[int] = set()
    for name, index in names.items():
        decision = decisions.get(name)
        known = decision is not None and decision.value is not None
        keep = known and decision.value == "yes"
        if keep:
            kept.add(index)
        record(make_receipt(
            FORK_COMPACTION, ("keep", "digest"), decision, fallback=not known,
            outcome="kept_verbatim" if keep else ("digested" if known else "digested_default"),
            extra={"message_index": index, "tool": str(rows[index].get("name") or "")},
        ))
    logger.debug("decision_forks: compaction kept %d/%d old tool results (%.0fms)",
                 len(kept), len(candidates), (time.monotonic() - started) * 1000)
    return kept


def verbatim_block(rows: Sequence[Dict[str, Any]], kept: Set[int], text_of) -> str:
    """The section of the compaction summary holding the kept results."""
    if not kept:
        return ""
    parts: List[str] = []
    budget = VERBATIM_CHARS_TOTAL
    for index in sorted(kept):
        row = rows[index]
        text = text_of(row.get("content")).strip()
        if len(text) > VERBATIM_CHARS_PER_RESULT:
            text = text[: VERBATIM_CHARS_PER_RESULT - 1].rstrip() + "…"
        if budget - len(text) < 0 and parts:
            break
        budget -= len(text)
        label = str(row.get("name") or "tool")
        parts.append(f"- TOOL {label} (kept verbatim):\n{text}")
    return "Kept verbatim (old tool results the work still needs):\n" + "\n".join(parts)


__all__ = [
    "DecisionTrace", "FORKS", "FORK_COMPACTION", "FORK_TOOL_ERROR", "FORK_TOOL_TIE",
    "MAX_ERROR_FORKS_PER_TURN", "PINNED_TAIL_MESSAGES", "adopt", "apply_tie_choice", "begin_turn",
    "choose_first_sync", "choose_verbatim", "current_trace", "decide_after_error",
    "error_hint", "failure_text", "fork_enabled", "followed_choice", "is_failed_result",
    "make_receipt", "record", "skips_error_fork", "stats", "tie_group", "tie_tolerance",
    "update_outcome", "verbatim_block",
]
