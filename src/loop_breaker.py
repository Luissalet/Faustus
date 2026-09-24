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

Weak local models rarely repeat the IDENTICAL call three times running —
that is easy for even a small model to notice itself. What they actually do
is OSCILLATE: read file A, read file B, read file A, read file B... (or a
three/four-step variant) with no net progress, which never trips the exact
streak above because no two consecutive calls are identical. The cycle
detector below watches the same bounded history of call signatures for a
short repeating period (2, 3 or 4 calls) instead of just "same as last
time", and escalates through the identical nudge/block/stop ladder, counted
on its own so a cycle never inflates (or is masked by) the exact-repeat
streak. A cycle whose signatures are all identical is just the exact-repeat
case by another name and is left to the streak path so nothing is counted
twice.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

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

#: Cycle detection: periods (in calls) checked for an oscillating pattern.
#: 2 covers the common A/B ping-pong; 3-4 cover slightly longer loops
#: (edit, run, revert, edit...). Anything longer is left alone — at that
#: length a "loop" is hard to distinguish from a legitimately long plan.
CYCLE_PERIODS = (2, 3, 4)
#: How many call signatures of history the cycle detector keeps around.
#: 24 is enough for the longest period (4) to show DEFAULT_CYCLE_MIN_REPEATS
#: repetitions several times over without growing unbounded per turn.
CYCLE_HISTORY_SIZE = 24
DEFAULT_CYCLE_DETECTION = True
DEFAULT_CYCLE_MIN_REPEATS_P2 = 3
DEFAULT_CYCLE_MIN_REPEATS_LONG = 2

#: Revisit detection: the same call again with only its numbers nudged
#: (a crop region shifted by a few hundredths, the same question each
#: time). Seen live: a vision question asked six times over overlapping
#: regions of one page, the answers never converging. A run of calls whose
#: arguments match once numbers are masked counts as circling when at least
#: REVISITS_TO_NUDGE of them land close to an earlier call of the run and
#: the run is RUN_TO_NUDGE long. Integers must match exactly to be "close"
#: (page 3 then page 4 is progress, not a revisit); other numbers within
#: REVISIT_TOLERANCE of each other are.
DEFAULT_REVISIT_RUN_TO_NUDGE = 4
DEFAULT_REVISITS_TO_NUDGE = 2
REVISIT_TOLERANCE = 0.12

_NUMBER_RE = __import__("re").compile(r"-?\d+(?:\.\d+)?")


def _mask_numbers(norm_args: str) -> Tuple[str, Tuple[float, ...], Tuple[bool, ...]]:
    numbers = _NUMBER_RE.findall(norm_args)
    values = tuple(float(n) for n in numbers)
    is_int = tuple("." not in n for n in numbers)
    return _NUMBER_RE.sub("#", norm_args), values, is_int


def _close(a: Tuple[float, ...], b: Tuple[float, ...], ints: Tuple[bool, ...]) -> bool:
    if len(a) != len(b):
        return False
    for x, y, integer in zip(a, b, ints):
        if integer:
            if x != y:
                return False
        elif abs(x - y) > REVISIT_TOLERANCE:
            return False
    return True


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
    #: Cycle (oscillation) detection — A→B→A→B... — settings. See the
    #: module docstring. Independent of the nudge/block/stop_after above,
    #: which only govern the exact-repeat streak.
    cycle_detection: bool = DEFAULT_CYCLE_DETECTION
    cycle_min_repeats_p2: int = DEFAULT_CYCLE_MIN_REPEATS_P2
    cycle_min_repeats_long: int = DEFAULT_CYCLE_MIN_REPEATS_LONG

    _last_signature: Optional[str] = field(default=None, repr=False)
    _streak: int = field(default=0, repr=False)
    _nudged_at: Optional[int] = field(default=None, repr=False)
    _blocked_at: Optional[int] = field(default=None, repr=False)
    blocked_tools: Set[str] = field(default_factory=set)
    # Bounded history of (tool, normalized_args, result_hash) signatures,
    # newest last, used only by the cycle detector — the exact-repeat streak
    # above never reads it. A different signature format from the streak's
    # joined string on purpose: a tuple compares cheaply and unambiguously
    # (no separator-collision risk) and lets messages name the tool/args
    # of each step in a detected cycle.
    _history: Deque[Tuple[str, str, str]] = field(default_factory=lambda: deque(maxlen=CYCLE_HISTORY_SIZE), repr=False)
    # Set (not reset by the exact-repeat streak, and never resets it) so a
    # turn's cycle-triggered nudge/block/stop is counted on a track of its
    # own — "one does not reset the other".
    last_trigger: Optional[str] = field(default=None, repr=False)
    last_cycle_period: Optional[int] = field(default=None, repr=False)
    last_cycle_repeats: Optional[int] = field(default=None, repr=False)
    last_cycle_reason: Optional[str] = field(default=None, repr=False)
    # Revisit track (see DEFAULT_REVISIT_*): masked signature of the current
    # run, the number vectors seen in it, how many were revisits, and
    # whether this run already got its one nudge.
    revisit_detection: bool = True
    _revisit_key: Optional[str] = field(default=None, repr=False)
    _revisit_vectors: List[Tuple[float, ...]] = field(default_factory=list, repr=False)
    _revisit_count: int = field(default=0, repr=False)
    _revisit_nudged: bool = field(default=False, repr=False)
    last_revisit_run: Optional[int] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # A misconfigured setting (block_after <= nudge_after, or a floor
        # below 1) must not make the ladder skip a rung or divide by zero —
        # clamp to a monotonic, at-least-one-apart sequence rather than
        # trusting the caller's setting values blindly.
        self.nudge_after = max(1, int(self.nudge_after or DEFAULT_NUDGE_AFTER))
        self.block_after = max(self.nudge_after + 1, int(self.block_after or DEFAULT_BLOCK_AFTER))
        self.stop_after = max(self.block_after + 1, int(self.stop_after or DEFAULT_STOP_AFTER))
        self.cycle_min_repeats_p2 = max(2, int(self.cycle_min_repeats_p2 or DEFAULT_CYCLE_MIN_REPEATS_P2))
        self.cycle_min_repeats_long = max(2, int(self.cycle_min_repeats_long or DEFAULT_CYCLE_MIN_REPEATS_LONG))
        if not isinstance(self._history, deque) or self._history.maxlen != CYCLE_HISTORY_SIZE:
            self._history = deque(self._history, maxlen=CYCLE_HISTORY_SIZE)

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
            cycle_detection=bool(get_setting("agent_loop_breaker_cycle_detection", DEFAULT_CYCLE_DETECTION)),
            cycle_min_repeats_p2=int(get_setting(
                "agent_loop_breaker_cycle_min_repeats_p2", DEFAULT_CYCLE_MIN_REPEATS_P2,
            ) or DEFAULT_CYCLE_MIN_REPEATS_P2),
            cycle_min_repeats_long=int(get_setting(
                "agent_loop_breaker_cycle_min_repeats_long", DEFAULT_CYCLE_MIN_REPEATS_LONG,
            ) or DEFAULT_CYCLE_MIN_REPEATS_LONG),
        )

    def observe(self, tool: str, args: Any, result_hash: str) -> str:
        """Record one (tool, args, result) observation and return the action
        to take: one of :data:`ACTIONS`. Pure — no I/O, no randomness, no
        dependence on any text the model produced about its own state. A
        signature identical to the previous call's extends the streak; ANY
        difference (a different tool, different arguments, or the same call
        returning a different result) resets it to a fresh streak of one,
        which is what makes a turn WITH real progress never trip this."""
        norm_args = normalize_args(args)
        signature = f"{tool}␟{norm_args}␟{result_hash}"
        prefix = f"{tool}␟{norm_args}␟"
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
        streak_action = self._streak_action(tool)
        self._history.append((tool, norm_args, result_hash))
        revisit = self._observe_revisit(tool, norm_args)
        action = self._finalize(tool, streak_action)
        if action == "none" and revisit:
            self.last_trigger = "revisit"
            return "nudge"
        return action

    def _observe_revisit(self, tool: str, norm_args: str) -> bool:
        """Advance the revisit track; True exactly once per run, when it
        first qualifies as circling."""
        if not self.revisit_detection:
            return False
        masked, values, ints = _mask_numbers(norm_args)
        key = f"{tool}\u241f{masked}"
        if key != self._revisit_key or not values:
            self._revisit_key = key if values else None
            self._revisit_vectors = [values] if values else []
            self._revisit_count = 0
            self._revisit_nudged = False
            return False
        if any(_close(values, earlier, ints) for earlier in self._revisit_vectors):
            self._revisit_count += 1
        self._revisit_vectors.append(values)
        run = len(self._revisit_vectors)
        if (not self._revisit_nudged and run >= DEFAULT_REVISIT_RUN_TO_NUDGE
                and self._revisit_count >= DEFAULT_REVISITS_TO_NUDGE):
            self._revisit_nudged = True
            self.last_revisit_run = run
            return True
        return False

    def observe_skipped(self, tool: str, args: Any) -> str:
        """A duplicate the runtime refused to execute (so there is no result
        to hash) still extends the streak when its tool+args match the last
        observed call: not running it is not progress. Returns the same
        actions as :meth:`observe`; a call to a different tool/args starts
        a fresh streak with an unknown result."""
        norm_args = normalize_args(args)
        prefix = f"{tool}␟{norm_args}␟"
        if self._last_signature and self._last_signature.startswith(prefix):
            self._streak += 1
        else:
            self._last_signature = prefix + "<skipped>"
            self._streak = 1
            self._nudged_at = None
            self._blocked_at = None
        streak_action = self._streak_action(tool)
        self._history.append((tool, norm_args, "<skipped>"))
        return self._finalize(tool, streak_action)

    def _streak_action(self, tool: str) -> str:
        """The exact-repeat ladder verdict for the streak counter as it
        stands right now — shared by :meth:`observe` and
        :meth:`observe_skipped`, which only differ in how they advance
        ``_streak``. Does not touch cycle state."""
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

    def _finalize(self, tool: str, streak_action: str) -> str:
        """Combine the exact-repeat streak verdict with the cycle detector's,
        with the streak winning ties (it is the narrower, more certain
        signal). Always records which track fired last, so a caller building
        a message can ask for the cycle-specific wording when relevant."""
        if streak_action != "none":
            self.last_trigger = "streak"
            self.last_cycle_period = None
            self.last_cycle_repeats = None
            self.last_cycle_reason = None
            return streak_action
        if not self.cycle_detection:
            self.last_trigger = None
            return "none"
        cycle_action = self._cycle_action(tool)
        if cycle_action != "none":
            self.last_trigger = "cycle"
            return cycle_action
        self.last_trigger = None
        self.last_cycle_period = None
        self.last_cycle_repeats = None
        self.last_cycle_reason = None
        return "none"

    def _min_repeats_for(self, period: int) -> int:
        return self.cycle_min_repeats_p2 if period == 2 else self.cycle_min_repeats_long

    def _detect_cycle(self) -> Optional[Tuple[int, int, List[Tuple[str, str, str]]]]:
        """Scan the tail of ``_history`` for a repeating period-``p`` pattern,
        p in :data:`CYCLE_PERIODS`, smallest period first. Returns
        ``(period, repeats, unit)`` for the first period that has completed
        at least its configured minimum number of full repetitions at the
        very end of the history, with the ``p`` signatures inside one
        repetition not all identical (that degenerate case is the
        exact-repeat streak's job, not this detector's — counting it here
        too would double-count the same non-progress). ``None`` when nothing
        in the checked periods qualifies."""
        history = list(self._history)
        n = len(history)
        for period in CYCLE_PERIODS:
            min_repeats = self._min_repeats_for(period)
            max_r = n // period
            if max_r < min_repeats:
                continue
            unit = history[n - period:n]
            # Degenerate on (tool, args) alone, ignoring the result hash: a
            # period-p "cycle" whose every step is the SAME call (whatever
            # its result) is the exact-repeat streak's job, not this
            # detector's — a skipped duplicate landing between two real
            # observations of that one call must not turn it into a fake
            # 2-cycle. A real alternation always has more than one distinct
            # (tool, args) pair in its unit.
            if len({(t, a) for t, a, _h in unit}) <= 1:
                continue
            repeats = 1
            for i in range(2, max_r + 1):
                chunk = history[n - i * period: n - (i - 1) * period]
                if self._chunk_matches(chunk, unit):
                    repeats += 1
                else:
                    break
            if repeats >= min_repeats:
                return period, repeats, unit
        return None

    @staticmethod
    def _chunk_matches(chunk: List[Tuple[str, str, str]], unit: List[Tuple[str, str, str]]) -> bool:
        """Two same-length windows are "the same repetition" for cycle
        purposes when every step's (tool, args) matches and the results
        match too — UNLESS one of the two was a runtime-skipped duplicate
        (no result to compare, marked "<skipped>"), which must not by
        itself break an otherwise-identical repetition: the exact-repeat
        streak already treats a skip-then-real transition the same way
        (see ``observe``'s own docstring)."""
        if len(chunk) != len(unit):
            return False
        for (c_tool, c_args, c_hash), (u_tool, u_args, u_hash) in zip(chunk, unit):
            if c_tool != u_tool or c_args != u_args:
                return False
            if c_hash != u_hash and c_hash != "<skipped>" and u_hash != "<skipped>":
                return False
        return True

    def _cycle_action(self, tool: str) -> str:
        found = self._detect_cycle()
        if found is None:
            return "none"
        period, repeats, unit = found
        min_repeats = self._min_repeats_for(period)
        block_delta = self.block_after - self.nudge_after
        stop_delta = self.stop_after - self.nudge_after
        block_min = min_repeats + block_delta
        stop_min = min_repeats + stop_delta
        self.last_cycle_period = period
        self.last_cycle_repeats = repeats
        self.last_cycle_reason = self._describe_cycle(period, repeats, unit)
        if repeats >= stop_min:
            return "stop"
        if repeats >= block_min:
            for step_tool, _args, _hash in unit:
                self.blocked_tools.add(step_tool)
            return "block_tool"
        if repeats >= min_repeats:
            return "nudge"
        return "none"

    @staticmethod
    def _describe_step(signature: Tuple[str, str, str]) -> str:
        tool, args, _result_hash = signature
        short_args = args if len(args) <= 40 else args[:37] + "..."
        return f"{tool}({short_args})" if short_args else tool

    def _describe_cycle(self, period: int, repeats: int, unit: List[Tuple[str, str, str]]) -> str:
        steps = [self._describe_step(sig) for sig in unit]
        if period == 2:
            return (
                f"alternating between {steps[0]} and {steps[1]} {repeats} times "
                "with the same results — change approach"
            )
        return (
            f"cycling through {period} repeated actions ({' → '.join(steps)}) {repeats} times "
            "with the same results — change approach"
        )

    def reset(self) -> None:
        """Explicit reset for a caller that knows progress happened through
        a channel `observe` does not see itself (e.g. a file mutation
        confirmed by the harness, not just a different tool result)."""
        self._last_signature = None
        self._streak = 0
        self._nudged_at = None
        self._blocked_at = None
        self.blocked_tools.clear()
        self._history.clear()
        self.last_trigger = None
        self.last_cycle_period = None
        self.last_cycle_repeats = None
        self.last_cycle_reason = None

    @property
    def streak(self) -> int:
        return self._streak

    def snapshot(self) -> Dict[str, Any]:
        return {
            "streak": self._streak, "blocked_tools": sorted(self.blocked_tools),
            "nudge_after": self.nudge_after, "block_after": self.block_after,
            "stop_after": self.stop_after,
            "last_trigger": self.last_trigger,
            "cycle_period": self.last_cycle_period,
            "cycle_repeats": self.last_cycle_repeats,
            "cycle_reason": self.last_cycle_reason,
        }
