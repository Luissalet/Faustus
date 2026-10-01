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
import re
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


#: A string field at least this long is free text (compared by likeness);
#: shorter ones (paths, actions, ids) must match exactly.
FREE_TEXT_MIN_CHARS = 40


def _split_free_text(norm_args: str) -> Tuple[str, List[str]]:
    """(exact part, free-text fields) of a normalized argument string. Only
    a JSON object is split; anything else is all exact."""
    try:
        parsed = json.loads(norm_args)
    except (json.JSONDecodeError, ValueError):
        return norm_args, []
    if not isinstance(parsed, dict):
        return norm_args, []
    exact: Dict[str, Any] = {}
    texts: List[str] = []
    for k in sorted(parsed):
        v = parsed[k]
        if isinstance(v, str) and len(v) >= FREE_TEXT_MIN_CHARS:
            exact[k] = "<text>"
            texts.append(v)
        else:
            exact[k] = v
    return json.dumps(exact, sort_keys=True, ensure_ascii=True, separators=(",", ":")), texts


def _has_identity(exact_part: str) -> bool:
    """Whether the exact part names its target: a short string field such
    as a path or a URL, not only numbers, flags and free text."""
    try:
        parsed = json.loads(exact_part)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    return any(isinstance(v, str) and v != "<text>" and len(v) >= 5 for v in parsed.values())


def _similar(a: str, b: str) -> bool:
    import difflib
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= REVISIT_SIMILARITY


#: How alike two free-text fields must read to belong to one run (0-1).
REVISIT_SIMILARITY = 0.85


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


def observation_for_tool(tool: str, args: Any, result: Any) -> Tuple[Any, str]:
    """Return the call and result fingerprint seen by the loop policy.

    ``update_plan`` assigns a new revision on every call, including when the
    model writes the identical checklist again. That revision is bookkeeping,
    not progress; comparing it hid an otherwise exact runaway loop.
    """
    if tool == "update_plan" and isinstance(result, dict):
        update = result.get("plan_update")
        if isinstance(update, dict) and isinstance(update.get("plan"), str):
            meaningful = {key: value for key, value in update.items() if key != "revision"}
            return meaningful, hash_result({
                "plan_update": meaningful,
                "exit_code": result.get("exit_code"),
            })
    return args, hash_result(result)


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
    _revisit_texts: List[str] = field(default_factory=list, repr=False)
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
        _masked_all, values, ints = _mask_numbers(norm_args)
        exact_raw, texts = _split_free_text(norm_args)
        exact_part = _mask_numbers(exact_raw)[0]
        key = f"{tool}\u241f{exact_part}"
        # Short fields (a path, an action) must match exactly; long free
        # text (a question, a query) only has to read alike: live, the model
        # re-cropped the same corner of a page rephrasing its question each
        # time.
        # With a short identifying field (a path) the free text is just the
        # wording; without one (a shell command) the text IS the call, and it
        # has to read alike.
        same_run = (
            key == self._revisit_key
            and len(texts) == len(self._revisit_texts)
            and (_has_identity(exact_raw)
                 or all(_similar(a, b) for a, b in zip(texts, self._revisit_texts)))
        )
        if not same_run or not values:
            self._revisit_key = key if values else None
            self._revisit_texts = texts
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


# ---------------------------------------------------------------------------
# Three more ways a turn gets stuck (StuckWatch)
#
# LoopPolicy above sees TOOL CALLS go by. These three never look like a
# repeated call, so they get their own counters, bounded and deterministic
# like the rest of this module -- never read off what the model says about
# itself:
#
#   monologue     3 assistant rounds in a row that said something and called
#                 no tool, while the loop was still holding the turn open (a
#                 turn that simply answers ends at its first such round, so a
#                 second and a third can only happen when something -- a
#                 nudge, an open plan -- kept it going). Nothing is moving:
#                 the first time the caller asks the advisor (or nudges) to
#                 act or finish; if it happens again the turn stops with what
#                 was said instead of nudging forever.
#   context       the provider refused the request as too long. The first
#                 time the caller compacts (harder than usual) and redoes the
#                 round; a second one in a row means compaction did not help,
#                 so stop with a clear message instead of retrying forever.
#   failed path   the same call (tool + arguments) returned an error three
#                 times: that path is closed, the fourth identical attempt is
#                 refused without running and the model is told to change the
#                 arguments or the approach. LoopPolicy nudges at three
#                 IDENTICAL results and only blocks the whole tool at six;
#                 this closes the one failing path earlier and leaves the tool
#                 (and any other arguments) alone.
# ---------------------------------------------------------------------------

MONOLOGUE_STOP_REASON = "monologue"
CONTEXT_ERROR_STOP_REASON = "context_length_exceeded"
DEFAULT_MONOLOGUE_ROUNDS = 3
DEFAULT_CONTEXT_ERROR_LIMIT = 2
DEFAULT_FAILED_PATH_LIMIT = 3
#: Bounded: a path table never grows past this many distinct failing calls.
MAX_TRACKED_PATHS = 64

_CONTEXT_ERROR_PATTERNS = (
    "context length", "context_length", "context window", "maximum context",
    "exceeds the context", "exceeded the context", "exceed the context",
    "prompt is too long", "prompt too long", "input is too long", "input too long",
    "too many tokens", "maximum prompt length", "max_model_len", "reduce the length",
    "request too large", "request_too_large", "tokens exceed", "token limit",
    "n_ctx", "exceeds available context", "context size", "longer than the maximum",
    "demasiado larg", "ventana de contexto",
)


def is_context_length_error(error: Any, status: Any = None) -> bool:
    """True when a provider error reads like "the request does not fit the
    context window": wording from the message (or the error class / code), not
    the bare HTTP status -- a 400 or 413 alone says nothing about why."""
    if isinstance(error, dict):
        text = " ".join(str(error.get(k) or "") for k in ("error", "message", "error_class", "code", "detail", "type"))
    else:
        text = str(error or "")
    text = text.lower()
    return any(p in text for p in _CONTEXT_ERROR_PATTERNS)


def is_failed_result(result: Any) -> bool:
    """True for a tool result that is a genuine failure: an error or a
    non-zero exit code. A policy block, an approval card and a question to the
    user are not failures of the call."""
    if not isinstance(result, dict):
        return False
    if result.get("blocked") or result.get("approval_required") or result.get("ask_user"):
        return False
    if result.get("error"):
        return True
    code = result.get("exit_code")
    return isinstance(code, int) and not isinstance(code, bool) and code != 0


@dataclass
class StuckWatch:
    """One instance per turn, like :class:`LoopPolicy`."""
    monologue_rounds: int = DEFAULT_MONOLOGUE_ROUNDS
    context_error_limit: int = DEFAULT_CONTEXT_ERROR_LIMIT
    failed_path_limit: int = DEFAULT_FAILED_PATH_LIMIT

    monologue_streak: int = field(default=0, repr=False)
    monologue_nudges: int = field(default=0, repr=False)
    context_error_streak: int = field(default=0, repr=False)
    _failures: Dict[Tuple[str, str], int] = field(default_factory=dict, repr=False)
    _closed: Set[Tuple[str, str]] = field(default_factory=set, repr=False)
    closed_paths_total: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        # 0 (or less) switches a detector off; anything else is clamped to a
        # sane floor so a typo cannot make the watch fire on the first event.
        self.monologue_rounds = 0 if int(self.monologue_rounds or 0) <= 0 else max(2, int(self.monologue_rounds))
        self.context_error_limit = 0 if int(self.context_error_limit or 0) <= 0 else max(2, int(self.context_error_limit))
        self.failed_path_limit = 0 if int(self.failed_path_limit or 0) <= 0 else max(2, int(self.failed_path_limit))

    @classmethod
    def from_settings(cls, get_setting=None) -> "StuckWatch":
        if get_setting is None:
            try:
                from src.settings import get_setting as _get_setting
                get_setting = _get_setting
            except Exception:
                get_setting = lambda key, default=None: default  # noqa: E731

        def _num(key: str, default: int) -> int:
            try:
                value = get_setting(key, default)
                return default if value is None else int(value)
            except (TypeError, ValueError):
                return default
        return cls(
            monologue_rounds=_num("agent_loop_breaker_monologue_rounds", DEFAULT_MONOLOGUE_ROUNDS),
            context_error_limit=_num("agent_loop_breaker_context_error_limit", DEFAULT_CONTEXT_ERROR_LIMIT),
            failed_path_limit=_num("agent_loop_breaker_failed_path_limit", DEFAULT_FAILED_PATH_LIMIT),
        )

    # -- monologue ---------------------------------------------------------
    def observe_round(self, *, tool_calls: int, has_text: bool) -> str:
        """Feed one finished model round. At the limit of consecutive
        text-only rounds the first answer is ``"nudge"`` (the caller asks the
        advisor, or says so itself, and the count starts again); reaching the
        limit a second time is ``"stop"``. A round with a tool call resets the
        count; an empty round (no text, no call) neither counts nor resets it
        -- the caller has its own handling for silence."""
        if tool_calls > 0:
            self.monologue_streak = 0
            return "none"
        if not has_text:
            return "none"
        self.monologue_streak += 1
        if self.monologue_rounds and self.monologue_streak >= self.monologue_rounds:
            if self.monologue_nudges == 0:
                self.monologue_nudges += 1
                self.monologue_streak = 0
                return "nudge"
            return "stop"
        return "none"

    # -- provider errors ---------------------------------------------------
    def observe_provider_error(self, error: Any) -> str:
        """Feed a provider error. ``"none"`` for any other kind of error,
        ``"compact"`` for the first context-length one, ``"stop"`` once
        ``context_error_limit`` of them came in a row."""
        if not is_context_length_error(error):
            self.context_error_streak = 0
            return "none"
        self.context_error_streak += 1
        if self.context_error_limit and self.context_error_streak >= self.context_error_limit:
            return "stop"
        return "compact"

    def observe_provider_ok(self) -> None:
        """A round the provider accepted breaks the context-error streak."""
        self.context_error_streak = 0

    # -- a failing path ----------------------------------------------------
    @staticmethod
    def _key(tool: str, args: Any) -> Tuple[str, str]:
        return str(tool or ""), normalize_args(args)

    def observe_call(self, tool: str, args: Any, failed: bool) -> bool:
        """Feed one executed call. True exactly when this call closed its
        path (the limit-th failure of the same tool + arguments). A success of
        the same call reopens it."""
        if not self.failed_path_limit:
            return False
        key = self._key(tool, args)
        if not failed:
            self._failures.pop(key, None)
            self._closed.discard(key)
            return False
        if len(self._failures) >= MAX_TRACKED_PATHS and key not in self._failures:
            self._failures.pop(next(iter(self._failures)))
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count
        if count >= self.failed_path_limit and key not in self._closed:
            self._closed.add(key)
            self.closed_paths_total += 1
            return True
        return False

    def path_closed(self, tool: str, args: Any) -> bool:
        return bool(self._closed) and self._key(tool, args) in self._closed

    def failures_for(self, tool: str, args: Any) -> int:
        return self._failures.get(self._key(tool, args), 0)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "monologue_streak": self.monologue_streak,
            "monologue_nudges": self.monologue_nudges,
            "context_error_streak": self.context_error_streak,
            "closed_paths": self.closed_paths_total,
            "monologue_rounds": self.monologue_rounds,
            "context_error_limit": self.context_error_limit,
            "failed_path_limit": self.failed_path_limit,
        }


# ── zero-token advisories ──────────────────────────────────────────────────
#
# Small, mechanical hints appended to a TOOL RESULT (never to the system
# prompt or to an earlier message, so the cached prompt prefix stays
# byte-identical). Each rule fires at most once per run. They cost no model
# call: plain pattern checks over the call and its result.

ADVISORY_GUARDS_SETTING = "agent_advisory_guards"

ADVISORY_RESCAN = (
    "[advisory] You already have results for a broad search of this tree from the last round "
    "or two. Narrow the next search (a specific folder or pattern) or read the file instead "
    "of scanning the same tree again."
)
ADVISORY_BATCH_EDITS = (
    "[advisory] Several edits in a row have the same shape on different files. Do the rest "
    "in one step with code_mode (or a short python script) instead of one call per file."
)
ADVISORY_SHELL_PIPELINE = (
    "[advisory] A find/wc/sort/xargs pipeline over the tree is slow and easy to get wrong "
    "here. Do this with code_mode or a short python script instead."
)

_EDIT_TOOLS = frozenset({"edit_file", "write_file"})
_SHELL_TOOLS = frozenset({"bash", "powershell"})
_SEARCH_WINDOW_ROUNDS = 2
_EDIT_STREAK = 3
_EDIT_SHAPE_RATIO = 0.9
_NO_RESULT_PREFIXES = ("(no output)", "no matches", "no files", "no results", "0 matches")

_SHELL_SEARCH_RE = re.compile(
    r"(?:^|[;&|(]\s*)(?:git\s+grep\b|grep\s+-[A-Za-z]*[rR]|rg\b|find\b|ls\s+-[A-Za-z]*R|"
    r"Get-ChildItem\b[^|;\n]*-Recurse|Select-String\b[^|;\n]*-Path)",
    re.I,
)
_SHELL_PIPELINE_RE = re.compile(
    r"(?:\bfind\b|\bls\s+-[A-Za-z]*R|Get-ChildItem\b[^|\n]*-Recurse)[^\n]*"
    r"\|\s*(?:xargs|wc|sort|uniq|Measure-Object|Sort-Object|ForEach-Object)\b",
    re.I,
)


def _call_args(tool: str, content: Any) -> Dict[str, Any]:
    if isinstance(content, dict):
        return content
    text = str(content or "").strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    if tool in _SHELL_TOOLS:
        return {"command": text}
    return {"raw": text}


def _shell_text(args: Dict[str, Any]) -> str:
    return str(args.get("command") or args.get("script") or args.get("cmd") or args.get("raw") or "")


def _norm_root(path: Any) -> str:
    p = str(path or "").replace("\\", "/").strip().strip("\"'")
    while p.startswith("./"):
        p = p[2:]
    p = p.rstrip("/")
    return p or "."


def _looks_like_file(root: str) -> bool:
    base = root.rsplit("/", 1)[-1]
    return root != "." and "." in base.lstrip(".") and not base.endswith(".")


def _shell_search_root(cmd: str) -> Optional[str]:
    """Root of a recursive shell search, or None when the command is not one."""
    m = _SHELL_SEARCH_RE.search(cmd)
    if not m:
        return None
    segment = re.split(r"[|;&]", cmd[m.start():].lstrip(";&|( \t"), maxsplit=1)[0]
    toks = [t.strip("\"'") for t in segment.split()]
    if not toks:
        return "."
    head = toks[0].lower()
    rest = toks[1:]
    if head == "find":
        return _norm_root(rest[0]) if rest and not rest[0].startswith("-") else "."
    if head == "get-childitem":
        for i, t in enumerate(rest):
            if t.lower() in ("-path", "-literalpath") and i + 1 < len(rest):
                return _norm_root(rest[i + 1])
        pos = [t for t in rest if not t.startswith("-")]
        return _norm_root(pos[0]) if pos else "."
    pos = [t for t in rest if not t.startswith("-")]
    if head in ("grep", "rg", "git", "select-string"):
        if head == "git":
            pos = pos[1:]               # drop the `grep` sub-command word
        return _norm_root(pos[-1]) if len(pos) >= 2 else "."
    return _norm_root(pos[-1]) if pos else "."          # ls -R <dir>


def _result_has_output(result: Any) -> bool:
    if not isinstance(result, dict):
        return bool(result)
    if result.get("error") or result.get("blocked"):
        return False
    code = result.get("exit_code")
    if isinstance(code, int) and code not in (0, 1):
        return False
    parts = [result.get(k) for k in ("output", "results", "matches", "stdout", "files")]
    text = " ".join(str(p) for p in parts if p).strip()
    return bool(text) and not text.lower().startswith(_NO_RESULT_PREFIXES)


def _alike(a: str, b: str) -> bool:
    if a == b:
        return True
    import difflib
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sm.real_quick_ratio() >= _EDIT_SHAPE_RATIO and sm.ratio() >= _EDIT_SHAPE_RATIO


class AdvisoryGuard:
    """Per-run state for the advisories. ``advise`` returns the text to append
    to this call's tool result ("" for none) and never raises."""

    def __init__(self) -> None:
        self.fired: Set[str] = set()
        self._last_search: Optional[Tuple[int, str]] = None
        self._edit_tool = ""
        self._edit_shape = ""
        self._edit_paths: List[str] = []

    # -- public ---------------------------------------------------------
    def advise(self, tool: str, args: Any, result: Any, round_num: int = 0) -> str:
        try:
            return self._advise(str(tool or ""), _call_args(str(tool or ""), args), result, int(round_num or 0))
        except Exception:  # noqa: BLE001 - an advisory must never cost a turn
            return ""

    # -- rules ----------------------------------------------------------
    def _advise(self, tool: str, args: Dict[str, Any], result: Any, round_num: int) -> str:
        out = ""
        if tool in _EDIT_TOOLS:
            out = self._edit_rule(tool, args)
        else:
            self._edit_tool, self._edit_shape, self._edit_paths = "", "", []
            if tool in _SHELL_TOOLS and "C" not in self.fired and _SHELL_PIPELINE_RE.search(_shell_text(args)):
                self.fired.add("C")
                out = ADVISORY_SHELL_PIPELINE
            search = self._search_rule(tool, args, result, round_num)
            out = out or search
        return out

    def _edit_rule(self, tool: str, args: Dict[str, Any]) -> str:
        path = str(args.get("path") or "")
        shape = json.dumps({k: v for k, v in args.items() if k not in ("path", "base_revision")},
                           sort_keys=True, default=str)[:3000]
        if (path and tool == self._edit_tool and path not in self._edit_paths
                and _alike(shape, self._edit_shape)):
            self._edit_paths.append(path)
        else:
            self._edit_tool, self._edit_shape = tool, shape
            self._edit_paths = [path] if path else []
        if len(self._edit_paths) >= _EDIT_STREAK and "B" not in self.fired:
            self.fired.add("B")
            return ADVISORY_BATCH_EDITS
        return ""

    def _search_root(self, tool: str, args: Dict[str, Any]) -> Optional[str]:
        """Root of a BROAD search (a whole tree, not one file), else None."""
        if tool == "grep":
            root = _norm_root(args.get("path"))
        elif tool == "glob":
            pattern = str(args.get("pattern") or "")
            if not any(c in pattern for c in "*?["):
                return None             # one named file, not a sweep
            root = _norm_root(args.get("path"))
        elif tool in _SHELL_TOOLS:
            root = _shell_search_root(_shell_text(args))
            if root is None:
                return None
        else:
            return None
        return None if _looks_like_file(root) else root

    def _search_rule(self, tool: str, args: Dict[str, Any], result: Any, round_num: int) -> str:
        root = self._search_root(tool, args)
        if root is None:
            return ""
        out = ""
        prev = self._last_search
        if (prev is not None and "A" not in self.fired
                and round_num - prev[0] <= _SEARCH_WINDOW_ROUNDS
                and _covers(root, prev[1])):
            self.fired.add("A")
            out = ADVISORY_RESCAN
        self._last_search = (round_num, root) if _result_has_output(result) else None
        return out


def _covers(new_root: str, old_root: str) -> bool:
    """The new search sweeps the old one's tree again (same folder or an
    ancestor). A search of a SUBfolder is narrowing, which is what we want."""
    if new_root == "." or new_root == old_root:
        return True
    return old_root.startswith(new_root.rstrip("/") + "/")


def advisory_guard_from_settings(get_setting=None) -> Optional[AdvisoryGuard]:
    """A fresh guard for one run, or None when `agent_advisory_guards` is off."""
    if get_setting is None:
        try:
            from src.settings import get_setting as _gs
            get_setting = _gs
        except Exception:  # noqa: BLE001
            get_setting = lambda key, default=None: default  # noqa: E731
    try:
        if not get_setting(ADVISORY_GUARDS_SETTING, True):
            return None
    except Exception:  # noqa: BLE001
        pass
    return AdvisoryGuard()


def advise(guard: Optional[AdvisoryGuard], tool: str, args: Any, result: Any, round_num: int = 0) -> str:
    """Advisory text for this call's result, or "" (also when `guard` is None)."""
    return guard.advise(tool, args, result, round_num) if guard is not None else ""


def with_advisory(text: str, advisory: str) -> str:
    """`text` with the advisory appended as its own paragraph (unchanged when empty)."""
    return f"{text}\n\n{advisory}" if advisory else text