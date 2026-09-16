"""loop_breaker.py — deterministic, bounded escalation for a non-progressing
turn (A29).

Faustus already has loop DETECTION and one form of RECOVERY inline in
``src/agent_loop.py`` (FAUSTUS.md §87 — ``_loop_recovery_*``, the
``_stuck_rounds``/``_call_freq``/``_same_probe_*`` counters around round
handling): it notices a repeated call, hides the offending tool from the next
schema and injects a system message telling the model to try something else.
That mechanism is itself already independent of the model "recognizing" the
loop — the counters are mechanical — but it lives inline in a 12,000-line
file this lot does not own, it never actually STOPS a turn, and its
escalation ladder is not a reusable, independently testable policy.

This module is that policy, pulled out into something with no dependency on
``agent_loop.py``, streaming, or an LLM: a pure state machine that watches
(tool, normalized_args, result_hash) triples go by and says what to do next.
``T7_wiring.md`` has the exact diff to call it from ``agent_loop.py`` in
place of (or alongside) the existing inline counters — out of this lot's file
ownership, so it is not applied here.

Escalation ladder, strictly bounded and counted, never inferred from the
model's own text:

    N times the SAME (tool, args, result) in a row  → "nudge"   (inject a
                                                         system message)
    another M times (still identical)                → "block_tool" (hide
                                                         that tool from the
                                                         next schema)
    another K times (STILL identical)                 → "stop"   (end the
                                                         turn — the caller's
                                                         stop_reason should be
                                                         "non_progressing_loop")

A single call whose result differs — even a call that repeats the tool and
arguments but returns something new — resets the streak to zero: that is
progress, not a loop, and must never be penalized the way repeating the
identical thing three times running does the acceptance case ("cycling
through diagnostics without advancing the task" IS the distinguishing
feature — see the module docstring of ``agent_loop.py``'s inline sibling for
the same rule stated the other way round).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

#: Escalation actions, in the order a non-progressing streak passes through
#: them. `none` means "nothing unusual yet" — the normal, overwhelming
#: majority case.
ACTIONS = ("none", "nudge", "block_tool", "stop")

#: The stop_reason a caller that wires `LoopPolicy` into a real turn should
#: use when `observe` returns "stop" — the exact motive A29 names.
STOP_REASON = "non_progressing_loop"

DEFAULT_NUDGE_AFTER = 3
DEFAULT_BLOCK_AFTER = 6
DEFAULT_STOP_AFTER = 10


def normalize_args(args: Any) -> str:
    """A canonical string for "the same call again" — key order and
    whitespace must not hide a repeat, and an unparsable payload (not valid
    JSON) still normalizes to *something* stable rather than raising, since a
    tool call's raw content is caller-controlled text, not a trusted
    structure."""
    if isinstance(args, (dict, list)):
        try:
            return json.dumps(args, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return repr(args)
    if isinstance(args, str):
        text = args.strip()
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text
        return normalize_args(parsed)
    return repr(args)


def hash_result(result: Any) -> str:
    """A short, stable fingerprint of a tool result, for callers that have
    the raw result and want `observe`'s `result_hash` computed for them
    rather than hashing it themselves. Truncation-insensitive on purpose
    (first 4000 chars) — two multi-megabyte outputs that only differ near
    the end are still "the same shape of result" for loop-detection
    purposes, and hashing gigabytes on every round is not free."""
    if isinstance(result, (dict, list)):
        try:
            text = json.dumps(result, sort_keys=True, ensure_ascii=True, default=str)
        except (TypeError, ValueError):
            text = repr(result)
    else:
        text = str(result)
    return hashlib.sha256(text[:4000].encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class LoopPolicy:
    """One instance per turn (or per worker — a coordinator and its workers
    each get their own streak; a worker looping must not count toward its
    coordinator's, and vice versa). Call :meth:`observe` once per tool call,
    in order; it never looks at anything but its own running counters, so
    two turns never interfere through it.
    """
    nudge_after: int = DEFAULT_NUDGE_AFTER
    block_after: int = DEFAULT_BLOCK_AFTER
    stop_after: int = DEFAULT_STOP_AFTER

    _last_signature: Optional[str] = field(default=None, repr=False)
    _streak: int = field(default=0, repr=False)
    _nudged_at: Optional[int] = field(default=None, repr=False)
    _blocked_at: Optional[int] = field(default=None, repr=False)
    blocked_tools: Set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        # A misconfigured setting (block_after <= nudge_after, or a floor
        # below 1) must not make the ladder skip a rung or divide by zero —
        # clamp to a monotonic, at-least-one-apart sequence rather than
        # trusting the caller's setting values blindly.
        self.nudge_after = max(1, int(self.nudge_after or DEFAULT_NUDGE_AFTER))
        self.block_after = max(self.nudge_after + 1, int(self.block_after or DEFAULT_BLOCK_AFTER))
        self.stop_after = max(self.block_after + 1, int(self.stop_after or DEFAULT_STOP_AFTER))

    @classmethod
    def from_settings(cls, get_setting=None) -> "LoopPolicy":
        """Build a policy from the project's settings store (falls back to
        the defaults above if `src.settings` cannot be imported — a policy
        must still exist for a caller that has none configured)."""
        if get_setting is None:
            try:
                from src.settings import get_setting as _get_setting
                get_setting = _get_setting
            except Exception:
                get_setting = lambda key, default=None: default  # noqa: E731
        return cls(
            nudge_after=int(get_setting("agent_loop_breaker_nudge_after", DEFAULT_NUDGE_AFTER) or DEFAULT_NUDGE_AFTER),
            block_after=int(get_setting("agent_loop_breaker_block_after", DEFAULT_BLOCK_AFTER) or DEFAULT_BLOCK_AFTER),
            stop_after=int(get_setting("agent_loop_breaker_stop_after", DEFAULT_STOP_AFTER) or DEFAULT_STOP_AFTER),
        )

    def observe(self, tool: str, args: Any, result_hash: str) -> str:
        """Record one (tool, args, result) observation and return the action
        to take: one of :data:`ACTIONS`. Pure — no I/O, no randomness, no
        dependence on any text the model produced about its own state. A
        signature identical to the previous call's extends the streak; ANY
        difference (a different tool, different arguments, or the same call
        returning a different result) resets it to a fresh streak of one,
        which is what makes a turn WITH real progress never trip this."""
        signature = f"{tool}␟{normalize_args(args)}␟{result_hash}"
        prefix = f"{tool}␟{normalize_args(args)}␟"
        if signature == self._last_signature or (
            # The runtime skipped the previous identical call(s) — there was
            # no result to compare, so this real result continues the streak
            # and becomes the one later calls are compared against.
            self._last_signature is not None
            and self._last_signature == prefix + "<skipped>"
        ):
            self._last_signature = signature
            self._streak += 1
        else:
            self._last_signature = signature
            self._streak = 1
            self._nudged_at = None
            self._blocked_at = None
        if self._streak >= self.stop_after:
            return "stop"
        if self._streak >= self.block_after:
            if self._blocked_at is None:
                self._blocked_at = self._streak
                self.blocked_tools.add(tool)
            return "block_tool"
        if self._streak >= self.nudge_after:
            if self._nudged_at is None:
                self._nudged_at = self._streak
            return "nudge"
        return "none"

    def observe_skipped(self, tool: str, args: Any) -> str:
        """A duplicate the runtime refused to execute (so there is no result
        to hash) still extends the streak when its tool+args match the last
        observed call: not running it is not progress. Returns the same
        actions as :meth:`observe`; a call to a different tool/args starts
        a fresh streak with an unknown result."""
        prefix = f"{tool}␟{normalize_args(args)}␟"
        if self._last_signature and self._last_signature.startswith(prefix):
            self._streak += 1
        else:
            self._last_signature = prefix + "<skipped>"
            self._streak = 1
            self._nudged_at = None
            self._blocked_at = None
        if self._streak >= self.stop_after:
            return "stop"
        if self._streak >= self.block_after:
            if self._blocked_at is None:
                self._blocked_at = self._streak
                self.blocked_tools.add(tool)
            return "block_tool"
        if self._streak >= self.nudge_after:
            if self._nudged_at is None:
                self._nudged_at = self._streak
            return "nudge"
        return "none"

    def reset(self) -> None:
        """Explicit reset for a caller that knows progress happened through
        a channel `observe` does not see itself (e.g. a file mutation
        confirmed by the harness, not just a different tool result)."""
        self._last_signature = None
        self._streak = 0
        self._nudged_at = None
        self._blocked_at = None
        self.blocked_tools.clear()

    @property
    def streak(self) -> int:
        return self._streak

    def snapshot(self) -> Dict[str, Any]:
        return {
            "streak": self._streak, "blocked_tools": sorted(self.blocked_tools),
            "nudge_after": self.nudge_after, "block_after": self.block_after,
            "stop_after": self.stop_after,
        }
