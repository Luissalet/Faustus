"""tool_outcome.py — four outcomes for a tool call or a worker run.

A boolean "did it fail?" puts three very different things in one bucket, and
the one that hurts is the user pressing Stop: a cancelled worker was counted as
a failed one, so a job the user interrupted on purpose read as a job that broke,
and the per-model scorecard blamed the model for it.

    success         it did what it was asked
    expected_error  it correctly refused or failed in a way it is meant to:
                    blocked by policy, approval required, a guard block, a
                    command that exited non-zero
    cancelled       somebody stopped it — the user's Stop button, a cancelled
                    job, a session that was deleted. NOT a failure.
    panic           it broke in a way nothing planned for: an unhandled
                    exception, a crash, an internal error

`classify_result` reads the result dicts the repo already produces (``error``,
``exit_code``, ``blocked``, ``approval_required``…); `classify_status` reads the
status/stop_reason strings the run records already carry. Both are total: any
input yields an Outcome, never an exception.

Everything here is additive — callers keep every field they already write and
add ``outcome`` beside it. `enabled()` (setting ``agent_tool_outcomes``) is what
call sites check before changing any COUNT: with it off, the old "anything that
did not finish is an error" arithmetic stands unchanged.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Optional


class Outcome(str, Enum):
    """A `str` enum so it serialises into JSON payloads as its value."""

    SUCCESS = "success"
    EXPECTED_ERROR = "expected_error"
    CANCELLED = "cancelled"
    PANIC = "panic"

    def __str__(self) -> str:              # pragma: no cover - trivial
        return self.value


#: The outcomes that count against a run in a failure tally.
FAILURE_OUTCOMES = frozenset({Outcome.EXPECTED_ERROR, Outcome.PANIC})

# Statuses / stop reasons, as the repo writes them today.
_CANCELLED_STATES = frozenset({
    "cancelled", "canceled", "stopped", "stopped_by_user", "cancelling", "interrupted",
})
_SUCCESS_STATES = frozenset({"done", "complete", "complete_unverified", "ok", "success", "passed"})
_EXPECTED_STATES = frozenset({
    "timeout", "timed_out", "stalled", "blocked", "refused", "awaiting_user", "partial",
    "rounds_exhausted", "budget_exceeded", "loop_breaker", "intent_nudge_exhausted",
})
_PANIC_STATES = frozenset({"crashed", "panic"})

# Keys a result dict uses to say "I refused on purpose".
_EXPECTED_FLAGS = ("blocked", "approval_required", "requires_approval", "needs_approval",
                   "guard_block", "policy_block", "denied", "rejected")
_CANCELLED_FLAGS = ("cancelled", "canceled", "stopped_by_user")

# An error text that reads like the machinery itself broke.
_PANIC_RE = re.compile(
    r"traceback \(most recent call last\)|unhandled exception|internal (?:server )?error|"
    r"segmentation fault|fatal error|\bpanic(?:ked)?\b|\bcrashed\b|"
    r"^\s*[A-Z]\w*(?:Error|Exception)\s*:",
    re.I | re.M,
)
# An error text that reads like a deliberate refusal.
_EXPECTED_RE = re.compile(
    r"\bblocked\b|\brefus(?:ed|es)\b|approval|not allowed|not permitted|denied|"
    r"policy|owned by sub-agent|guard",
    re.I,
)


def enabled() -> bool:
    """Setting ``agent_tool_outcomes``. Off = the old failure arithmetic."""
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_tool_outcomes", True))
    except Exception:  # noqa: BLE001 - never raise into a hot path
        return True


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "none")
    return bool(value)


def classify_status(status: Any, *, error: Any = None, cancelled: bool = False) -> Outcome:
    """Outcome of a run/worker record from its status or stop_reason."""
    if cancelled:
        return Outcome.CANCELLED
    state = str(status or "").strip().lower()
    if state in _CANCELLED_STATES:
        return Outcome.CANCELLED
    text = str(error or "")
    if state in _PANIC_STATES:
        return Outcome.PANIC
    if state in _EXPECTED_STATES:
        return Outcome.EXPECTED_ERROR
    if text:
        return _classify_error_text(text)
    if state in _SUCCESS_STATES or not state:
        return Outcome.SUCCESS
    # "error", "failed", anything unknown: an unplanned end.
    return Outcome.PANIC if state in ("error", "failed") else Outcome.EXPECTED_ERROR


def _classify_error_text(text: str) -> Outcome:
    if _EXPECTED_RE.search(text):
        return Outcome.EXPECTED_ERROR
    if _PANIC_RE.search(text):
        return Outcome.PANIC
    return Outcome.EXPECTED_ERROR


def classify_result(result: Any, *, cancelled: bool = False) -> Outcome:
    """Outcome of one tool result. `cancelled=True` is the caller saying "this
    was stopped" (a cancelled task, the Stop button) and always wins."""
    if cancelled:
        return Outcome.CANCELLED
    if not isinstance(result, dict):
        # A tool that returned plain text ran to completion.
        return Outcome.SUCCESS
    for flag in _CANCELLED_FLAGS:
        if _truthy(result.get(flag)):
            return Outcome.CANCELLED
    state = str(result.get("status") or result.get("stop_reason") or "").strip().lower()
    if state in _CANCELLED_STATES:
        return Outcome.CANCELLED
    for flag in _EXPECTED_FLAGS:
        if _truthy(result.get(flag)):
            return Outcome.EXPECTED_ERROR
    error = result.get("error")
    code = result.get("exit_code")
    failed_code = False
    if isinstance(code, bool):
        failed_code = code
    elif isinstance(code, (int, float)):
        failed_code = int(code) != 0
    elif isinstance(code, str) and code.strip().lstrip("-").isdigit():
        failed_code = int(code.strip()) != 0
    if not error and not failed_code:
        if state and state not in _SUCCESS_STATES:
            return classify_status(state, error=None)
        return Outcome.SUCCESS
    if state in _PANIC_STATES:
        return Outcome.PANIC
    return _classify_error_text(str(error or "")) if error else Outcome.EXPECTED_ERROR


def counts_as_failure(outcome: Any) -> bool:
    """True for the two outcomes a failure tally should count. A cancelled run
    is not a failure; neither is a success."""
    try:
        value = Outcome(str(outcome))
    except (ValueError, TypeError):
        return False
    return value in FAILURE_OUTCOMES


def value_of(outcome: Any) -> Optional[str]:
    """The plain string of an Outcome (or of anything that looks like one)."""
    if outcome is None:
        return None
    try:
        return Outcome(str(outcome)).value
    except (ValueError, TypeError):
        return None
