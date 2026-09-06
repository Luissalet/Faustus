"""When the frontier has stopped producing work, and how the run says so.

Section 9's "marginalidad" and section 11's exits.  This module owns one
sentence and it is the one the whole subsystem is named for:

    **A stop for budget is never reported as convergence.**

`STOP_REASONS` keeps `converged` and `budget` apart, `HONEST_STOPS` names which
of them mean "there was nothing more worth doing", and
`CompletionDecision.parse` REFUSES an honest stop while any budget line is
exhausted.  That refusal is a last line of defence, and a last line of defence
that fires is already a bug in production: it raises at the close of a run, on
the object that was about to be written down, after all the work is done.
`stop_reason()` is the function whose job is to make sure it never fires --
it checks the budget BEFORE it consults the state, in every path.

The asymmetry is deliberate and it is worth naming.  "We ran out" reported as
"there was nothing left worth doing" is the single most misleading sentence
this engine could produce, because it is the one that stops anyone from raising
the budget: convergence invites no action, and running out invites exactly one.
The reverse error -- calling a genuine convergence a budget stop -- costs
somebody one wasted look at a budget that was fine.  The two are not equally
bad, so the check is not symmetric: exhaustion wins.

**On `src/convergence.py`, which already exists.**  It was read before this
module was written, and it is reused for the case it was built for rather than
reimplemented: `generative_stalled()` below is a thin delegation to its
`assess()`, because §9's last bullet asks for exactly that -- "usar detector de
convergencia para rondas generativas".

It is NOT used for `step()`, and the reason is a difference in the question,
not a difference in taste.  `src/convergence.py` reads the ARTIFACTS of
successive rounds and answers "are these rounds still changing anything?" from
token-set similarity and length trend.  That is the right question for a fix
loop that rewrites the same file until it settles.  The completion frontier
asks something else: "does anything still clear the marginal bar?"  That is a
threshold question about a SET OF CANDIDATES, and it has answers the text
detector would get exactly backwards -- a round that produced a large, novel,
worthless candidate looks like vigorous progress to a similarity metric and is
precisely the round on which a greedy engine must stop.  Feeding serialised
frontiers to a Jaccard comparison would also make convergence depend on
candidate ids, so one renamed id would read as "still moving".  Two different
questions, two detectors, and this docstring rather than a silent second copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

from src.completion_engine.budgeting import exhausted_lines
from src.completion_engine.contracts import (
    HONEST_STOPS,
    STOP_REASONS,
    CompletionBudget,
)

__all__ = [
    # Re-exported so that a caller asking "was this an honest stop?" reads the
    # vocabulary from the module that PRODUCED the stop reason.  Importing the
    # answer from here and the vocabulary from `contracts` is how two versions
    # of the same check end up in one codebase.
    "HONEST_STOPS",
    "STALL_ROUNDS",
    "STATE_REASONS",
    "ConvergenceState",
    "step",
    "converged",
    "stop_reason",
    "generative_stalled",
]


#: How many rounds of no progress mean the frontier has settled.  Two, not one:
#: a single round that executed nothing is ordinary -- everything left was
#: quarantined behind one dependency, or the batch was unaffordable this round
#: and will not be next -- and stopping on it would end runs that had work
#: left.  Two consecutive rounds is the point where "nothing happened" stops
#: being an accident.
STALL_ROUNDS = 2

#: The reasons `step` may write into the state.  These are NOT `STOP_REASONS`:
#: they describe what the ROUNDS did, and `converged`/`stop_reason` translate
#: them into the run's vocabulary.  Keeping them separate is what stops a
#: round-level observation from being written straight into a decision -- the
#: translation is where the budget gets the chance to overrule.
STATE_REASONS: Tuple[str, ...] = ("", "empty", "stalled", "max_rounds")


@dataclass(frozen=True)
class ConvergenceState:
    """What the rounds have done so far.  Spends and counts, never a verdict.

    `last_utility` is the best utility the previous frontier offered, and it is
    kept so that `step` can tell a round that found nothing from a round that
    found something and could not run it.  `reason` is a `STATE_REASONS` value
    and is an OBSERVATION -- the decision is `converged()`'s and the final word
    is `stop_reason()`'s, which is the only function here that sees the budget.
    """

    rounds: int = 0
    executed: int = 0
    last_utility: float = 0.0
    stalled_rounds: int = 0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rounds": self.rounds,
            "executed": self.executed,
            "last_utility": self.last_utility,
            "stalled_rounds": self.stalled_rounds,
            "reason": self.reason,
        }


def step(state: ConvergenceState, *, frontier: Any, executed_now: int,
         max_rounds: int) -> ConvergenceState:
    """One round later.  Counts what happened; decides nothing.

    A round is STALLED when it executed nothing, or when it executed something
    and the best remaining utility did not improve on the last one.  The second
    clause is the one that matters for a greedy engine: a run that keeps
    executing progressively worse candidates is not converging by any count of
    rounds, and only the trend in the bar it is clearing shows it.

    `max_rounds` is recorded as an observation (`max_rounds`) rather than acted
    on, because rounds are a SPENDABLE unit -- hitting the ceiling is running
    out, and running out is `stop_reason`'s to translate.  Deciding it here
    would put the budget conclusion in the one function that cannot see the
    budget.

    A frontier that is not a `Frontier` -- `None`, most likely, from a round
    that failed before building one -- counts as producing nothing rather than
    raising.  This runs on the turn path at the end of a round that may already
    have gone wrong, and an exception here would replace a recoverable stop
    with a crash.
    """
    try:
        selected = tuple(frontier.selected()) if frontier is not None else ()
        top = max((float(e.utility) for e in frontier.entries if e.admitted),
                  default=0.0) if frontier is not None else 0.0
    except Exception:  # noqa: BLE001 - a stop detector never raises
        selected, top = (), 0.0

    rounds = int(state.rounds) + 1
    executed = int(state.executed) + max(0, int(executed_now))
    progressed = int(executed_now) > 0 and top > float(state.last_utility)
    stalled = int(state.stalled_rounds) + (0 if progressed else 1)

    reason = ""
    if not selected:
        reason = "empty"
    elif stalled >= STALL_ROUNDS:
        reason = "stalled"
    if max_rounds > 0 and rounds >= int(max_rounds):
        # Checked last so it wins: a round that both settled and ran out has
        # run out, and rule 2 says that is the fact the user is owed.
        reason = "max_rounds"
    return ConvergenceState(rounds=rounds, executed=executed,
                            last_utility=round(float(top), 4),
                            stalled_rounds=stalled, reason=reason)


def converged(state: ConvergenceState) -> Tuple[bool, str]:
    """`(stop, STOP_REASON)` from the rounds alone.  The budget is not here.

    `empty` and `stalled` become `converged`: nothing left cleared the bar.
    `max_rounds` becomes `budget`, because rounds are a budget unit and a run
    that used its last one did not run out of ideas.

    This function is deliberately blind to the budget, and `stop_reason` is
    deliberately not.  Splitting them that way means the honest-stop check has
    exactly one home instead of being a condition every caller has to remember;
    calling `converged()` directly and believing it is the bug this module
    exists to make impossible, which is why `stop_reason` is what the closeout
    is meant to use and why this returns `(False, "")` rather than a stop when
    it has nothing to say.
    """
    if state.reason == "max_rounds":
        return True, "budget"
    if state.reason in ("empty", "stalled"):
        return True, "converged"
    return False, ""


def stop_reason(*, state: ConvergenceState,
                budget: Optional[CompletionBudget] = None,
                cancelled: bool = False, blocked: bool = False) -> str:
    """The run's single `STOP_REASONS` value.  **Never `converged` while a
    budget line is exhausted.**

    The order is the rule:

        cancelled -> blocked -> BUDGET -> whatever the rounds say

    The budget is consulted before the state, in every path, so there is no
    branch in which a settled frontier can outvote an exhausted line.  That is
    the invariant `CompletionDecision.parse` enforces from the other side, and
    this is the side that has to produce it correctly: the parse refusal fires
    at the close of the run, on the object about to be persisted, where the
    failure is most expensive and least useful.

    Both `HONEST_STOPS` are guarded, not just `converged`.  `core_only` is the
    other one, and it is refused by the same parse rule for the same reason --
    a literal run that ran out of rounds before proving its core stopped for
    budget, and reporting "we did the core and stopped" would be the same lie
    in a smaller voice.

    `budget` is returned ONLY when something actually ran out: either a line is
    exhausted, or the rounds ceiling was hit (`state.reason == "max_rounds"`,
    which IS the rounds line running out and stays true for a caller whose
    budget object does not count rounds).  A `budget` stop with nothing spent
    would send somebody to raise a limit that was never reached.

    `cancelled` and `blocked` outrank even the budget because they are facts
    about the world rather than about this run's arithmetic: a cancelled run
    did not converge and did not run out, it was stopped, and a run waiting on
    something outside itself has not finished deciding anything.
    """
    if cancelled:
        return "cancelled"
    if blocked:
        return "blocked"
    ran_out = bool(budget is not None and exhausted_lines(budget))
    if ran_out:
        return "budget"
    stop, reason = converged(state)
    if not stop:
        return ""
    # There is deliberately no second honest-stop guard in this branch.  The
    # `ran_out` return above is the only path an exhausted budget can take, so
    # a check here would be unreachable -- and an unreachable defence is worse
    # than none, because it reads as the place the rule lives and invites the
    # next reader to move the early return without noticing the rule left with
    # it.  `HONEST_STOPS` is imported and used by the tests that hold this line
    # from the outside, which is where a guarantee about an ORDER belongs.
    return reason if reason in STOP_REASONS else "budget"


def generative_stalled(artifacts: Sequence[Any]) -> Tuple[bool, str]:
    """Whether a GENERATIVE loop has settled, per §9's last bullet.

    A delegation to `src.convergence.assess`, which already answers this from
    the artifacts of successive rounds and has done since before this package
    existed.  It is imported here rather than reimplemented because the
    alternative -- a second similarity metric in this package -- would be two
    detectors that agree until one of them is tuned.

    This is the companion to `step()`, not a replacement for it: `step` reads
    the frontier's threshold question and this reads the text of what rounds
    produced.  See the module docstring for why the two cannot be the same
    function.

    Imported lazily and defensively.  This module is on the turn path and a
    stop detector that fails to import would take the close down with it; a run
    that cannot assess its generative rounds is one that reports "not settled"
    and keeps its other exits, which is the safe direction.
    """
    try:
        from src.convergence import assess

        verdict = assess(list(artifacts or ()))
        return bool(verdict.get("converged")), str(verdict.get("reason", ""))
    except Exception as exc:  # noqa: BLE001 - a stop detector never raises
        return False, f"convergence detector unavailable: {exc}"
