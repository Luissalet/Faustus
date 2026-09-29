"""Pure action controller for the existing round-extension gate.

No settings, application imports, permissions, or effects. The live loop uses
the valid action, keeps its grants and effects, and falls back to the legacy
condition when this controller fails or returns a malformed result.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class RoundExtensionInput:
    round_num: int
    rounds_budget: int
    grant_rounds: int
    cycles_left: int
    progress_enabled: bool
    progress_ceiling: int
    events_now: int
    events_last: int
    no_progress_streak: int
    recovery_active: bool


@dataclass(frozen=True)
class RoundExtensionDecision:
    action: str
    reason: str
    rounds_delta: int
    cycles_after: int
    streak_after: int
    progress_gate_open: bool


def decide_round_extension(state: RoundExtensionInput) -> RoundExtensionDecision:
    if state.round_num <= state.rounds_budget:
        return RoundExtensionDecision("not_due", "within_budget", 0,
                                      state.cycles_left, state.no_progress_streak, False)
    streak = 0 if state.events_now > state.events_last else state.no_progress_streak + 1
    progress = (state.progress_enabled and streak < 3
                and state.round_num <= state.progress_ceiling)
    if state.recovery_active:
        reason = "loop_recovery"
    elif state.cycles_left != 0:
        return RoundExtensionDecision("extend", "configured_cycle", state.grant_rounds,
                                      max(0, state.cycles_left - 1) if state.cycles_left > 0 else state.cycles_left,
                                      streak, progress)
    elif progress:
        return RoundExtensionDecision("extend", "progress", state.grant_rounds, 0, streak, True)
    elif not state.progress_enabled:
        reason = "cycles_exhausted"
    elif streak >= 3:
        reason = "no_progress"
    else:
        reason = "progress_ceiling"
    return RoundExtensionDecision("stop", reason, 0, state.cycles_left, streak, progress)


def matches_legacy(decision: RoundExtensionDecision, *, extends: bool, rounds_delta: int, cycles_after: int,
                   streak: int, progress_gate_open: bool) -> bool:
    """Compare observed legacy gates; never return a permission to continue."""
    return (decision.action == ("extend" if extends else "stop")
            and decision.rounds_delta == rounds_delta
            and decision.cycles_after == cycles_after
            and decision.streak_after == streak
            and decision.progress_gate_open == progress_gate_open)
