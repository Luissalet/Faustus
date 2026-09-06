"""Turning a mode into ceilings, and a candidate's 0..1 cost into units.

Section 9 of `inspiration/PLAN_GREEDY_COMPLETION_ENGINE_FAUSTUS.md`.
`completion_engine/contracts.py` owns `CompletionBudget` -- the ceilings, the
reserve and `may_spend` -- and this module owns everything the contract
deliberately left to a caller: which line funds which, how an estimator's
fraction becomes rounds and tokens, and what the interface is shown at the
close.

Four rules are mechanised here, each named with the failure it prevents:

1.  **The verification reserve is not spendable, in any mode.** The contract
    already refuses it in `CompletionBudget.may_spend`, because `ceiling()`
    gives `verification` the reserve and gives no other line any part of it.
    The job of this module is to not find a way around that, and the way
    around it would have been arithmetic of its own: a helper that computed
    "what is left overall" and handed it to a bonus candidate would have
    rebuilt the borrowing the contract forbids, one level up.  So nothing here
    ever sums across lines to answer an affordability question --
    `afford()` asks `may_spend` about ONE line and returns its answer.

2.  **`estimated_cost` is a FRACTION OF ITS LINE, never an absolute.**  §5.4
    types it 0..1 and never says of what.  Here it is said: a candidate with
    `estimated_cost = 0.08` would consume 8% of the budget line that funds it.
    `afford()` therefore derives the conversion from `budget.ceiling(...)` and
    a caller who wants a fixed scale passes `unit_costs` explicitly.  Without
    this sentence somebody multiplies 0.08 by a token budget of 200_000, gets
    16_000 tokens for a one-line test, and the frontier stops funding anything.

3.  **Sunk cost never justifies continuing** (§9).  No function in this module
    accepts, stores or reads "what has already been spent on THIS candidate".
    `afford()` is given a candidate and a budget and asks only what the NEXT
    spend would cost; there is no parameter it could read the history from.
    `tests/test_completion_engine_budget.py` fixes this by reading this
    module's own syntax tree, because a behavioural test would pass on the day
    somebody adds the parameter and only fail once something used it.

4.  **A line that is only an ALIAS of another shares that other's meter.**
    This is the one place where this module does arithmetic the contract does
    not, and it is doing it to keep the contract's own promise.  `ceiling()`
    says, in prose, that "recovery shares the core line rather than having its
    own" and that "exploration is funded from the bonus line rather than added
    to it" -- but it implements that as an equal CEILING while `used()` keeps a
    separate meter per line, so a run that spent its core line dry can spend
    the same amount again under `recovery`.  `FUNDING_LINE` resolves every line
    to the pot that actually funds it and `afford`, `record` and
    `exhausted_lines` all go through it, so a spend booked here is a spend the
    next question sees.  The frozen behaviour is reported, not patched:
    `tests/test_completion_engine_budget.py::test_frozen_budget_lines_double_count`
    is an `xfail(strict=True)` against the contract itself.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Tuple

from src.agent_profiles.completion import policy_for
from src.completion_engine.contracts import (
    BUDGET_LINES,
    SPENDABLE_UNITS,
    BudgetSpend,
    CompletionBudget,
    ImprovementCandidate,
)

__all__ = [
    "DEFAULT_RESERVE_SHARE",
    "DEFAULT_UNIT_COSTS",
    "FUNDING_LINE",
    "FUNDED_LINES",
    "funding_line",
    "budget_for",
    "from_settings",
    "estimate",
    "afford",
    "record",
    "report",
    "exhausted_lines",
]


#: §9's `verification_reserve` as a share of the turn.  The contract defaults
#: to the same number; it is restated here because this is the module callers
#: import, and a default that lives only in the class they never touch is a
#: default nobody can find.
DEFAULT_RESERVE_SHARE = 0.15

#: The reference size of ONE budget line, used only when a caller asks for an
#: estimate without a budget in hand.  Read it as: a candidate whose
#: `estimated_cost` is 1.0 -- one that would eat its whole line -- costs this
#: much.  These are not ceilings and they are not added to anything; they are
#: the denominator rule 2 says the 0..1 scale needs, in the case where no real
#: ceiling is available.  `afford()` never uses them: it has the budget, so it
#: uses the line's actual ceiling and this table stays out of the decision.
DEFAULT_UNIT_COSTS: Mapping[str, float] = {
    "rounds": 4.0,
    "tool_calls": 12.0,
    "tokens": 24000.0,
    "seconds": 180.0,
}

#: Which line's POT each budget line actually spends from.  Rule 4.
#:
#: `exploration` and `recovery` are views, not pots: the contract's `ceiling()`
#: hands them the same number as `bonus` and `core` respectively, and says in
#: prose that this is funding rather than addition.  Mapping them here is what
#: makes that prose true of the meter as well as of the ceiling.
FUNDING_LINE: Dict[str, str] = {
    "core": "core",
    "verification": "verification",
    "bonus": "bonus",
    "exploration": "bonus",
    "recovery": "core",
}

#: The lines that own a pot of their own.  Their ceilings partition the total
#: exactly -- core + verification + bonus == total, for every share -- which is
#: the property `report()` reports and the total-is-never-exceeded test reads.
FUNDED_LINES: Tuple[str, ...] = ("core", "verification", "bonus")

#: Units the contract stores as whole numbers.  `seconds` is the only float.
_WHOLE_UNITS: Tuple[str, ...] = ("rounds", "tool_calls", "tokens")


def funding_line(line: str) -> str:
    """The pot `line` spends from.  Unknown names are returned untouched.

    Returning the name rather than raising keeps this total: the caller that
    passes a bad line gets the contract's own `unknown budget line` refusal
    from `may_spend`, which names the value, instead of a second error from
    here that names the same value less usefully.
    """
    return FUNDING_LINE.get(str(line or ""), str(line or ""))


# -- building one ----------------------------------------------------------


def budget_for(*, mode: str, rounds: int = 0, tool_calls: int = 0,
               tokens: int = 0, seconds: float = 0.0,
               reserve_share: float = DEFAULT_RESERVE_SHARE) -> CompletionBudget:
    """The budget a mode implies over the ceilings a caller actually has.

    Every argument is a TOTAL for the turn, not a per-line allowance.  That is
    the denominator `SPENDABLE_UNITS` fixed and its comment states outright:

        "the share is OF THE TOTAL of each unit, so `greedy` may spend a third
        of the turn's rounds, tokens, seconds and tool calls on bonus work,
        and not a third of whatever happens to be left when core finishes."

    A unit left at 0 is a unit this budget does not count -- not a unit with a
    ceiling of zero.  `may_spend` skips it and `exhausted` skips it, so a run
    given only `rounds` is bounded by rounds alone rather than being refused
    everything for having no token ceiling.

    `mode` is resolved through `agent_profiles.completion.policy_for`, which
    refuses an unknown name.  A silent fallback to `greedy` would answer a typo
    by authorising the widest bonus share this engine has.
    """
    policy = policy_for(mode)
    total = BudgetSpend(rounds=max(0, int(rounds)),
                        tool_calls=max(0, int(tool_calls)),
                        tokens=max(0, int(tokens)),
                        seconds=max(0.0, float(seconds)))
    return CompletionBudget.for_policy(policy, total, reserve_share=reserve_share)


def from_settings(*, mode: str, max_rounds: int, max_tool_calls: int = 0,
                  token_budget: int = 0,
                  wall_seconds: float = 0.0) -> CompletionBudget:
    """The same budget, read off the names a run's settings already use.

    This is the seam where `CompletionPolicy.bonus_budget_share` finally gets
    its denominator.  The field has been `0.15 / 0.35 / 0.60` in
    `agent_profiles/completion.py` for weeks with no statement anywhere of what
    it is a share OF, and the contract answered it in the comment beside
    `SPENDABLE_UNITS`, quoted in `budget_for` above: the total of each unit,
    for the whole turn.

    That choice is load-bearing rather than cosmetic.  A share of the REMAINDER
    would mean a run that overspent on core got a bigger bonus allowance than
    one that came in under -- the incentive exactly inverted -- and the
    inversion would only ever show up as "why did the sloppy run explore
    more?", months later, with nothing in the record to answer it.

    `max_rounds` has no default because it is the one ceiling every run in this
    repository already has; defaulting it to zero would quietly produce a
    budget that counts nothing and therefore never stops anything.
    """
    return budget_for(mode=mode, rounds=max_rounds, tool_calls=max_tool_calls,
                      tokens=token_budget, seconds=wall_seconds)


# -- what one candidate would cost -----------------------------------------


def estimate(candidate: ImprovementCandidate, *,
             unit_costs: Mapping[str, float] | None = None) -> BudgetSpend:
    """`candidate.estimated_cost` (0..1) converted into units.

    **The scale.**  `estimated_cost` is a FRACTION OF THE BUDGET LINE that
    would fund this candidate, never an absolute quantity.  `0.08` means "about
    eight percent of the bonus line", so the same candidate is cheaper in a
    generous turn and dearer in a mean one, which is the only reading under
    which a single 0..1 number can survive being carried between runs with
    different ceilings.  `unit_costs` is the size of one whole line: what a
    candidate scoring 1.0 would spend.  `afford()` passes the line's real
    ceiling; `DEFAULT_UNIT_COSTS` is the fallback for a caller with no budget
    in hand, and it is a reference size rather than a limit.

    Say it once more because the alternative reading is the expensive one: this
    function must never be handed a TOTAL token budget as `unit_costs` unless
    that total really is the line, or a 0.08 candidate is billed eight percent
    of the whole turn for a one-line regression test.

    Whole units round UP, and only away from zero.  Rounding a 0.4-round
    candidate down to 0 would make it free, and free candidates never exhaust
    anything -- an unbounded number of them would run inside a budget that
    reported itself untouched the whole time.
    """
    costs = dict(DEFAULT_UNIT_COSTS if unit_costs is None else unit_costs)
    share = float(candidate.estimated_cost or 0.0)
    if share < 0.0:
        share = 0.0
    values: Dict[str, Any] = {}
    for unit in SPENDABLE_UNITS:
        size = float(costs.get(unit, 0.0) or 0.0)
        raw = max(0.0, size * share)
        if unit in _WHOLE_UNITS:
            values[unit] = int(math.ceil(raw)) if raw > 0 else 0
        else:
            values[unit] = round(raw, 3)
    return BudgetSpend(rounds=values["rounds"], tool_calls=values["tool_calls"],
                       tokens=values["tokens"], seconds=values["seconds"])


def afford(budget: CompletionBudget, line: str, candidate: ImprovementCandidate,
           *, unit_costs: Mapping[str, float] | None = None) -> Tuple[bool, str]:
    """`(ok, reason)` for spending `candidate` against `line`.  Rules 1 and 3.

    The reason is `""` when affordable and `"budget"` -- a `REJECTION_REASONS`
    value, so a caller can store it on the candidate without translating --
    when it is not.  The contract's own sentence is kept in `detail` by callers
    that want it; what travels is the closed vocabulary.

    Three things this function does NOT do, each on purpose:

    * it never sums two lines to find room.  `may_spend` is asked about the ONE
      pot `line` draws from, so the verification reserve cannot be reached from
      here by any argument -- rule 1.  There is no branch in which `line`
      resolves to `verification` unless the caller asked for verification.
    * it never reads what this candidate has already consumed.  There is no
      parameter for it and no lookup of one; the question is only what the NEXT
      spend costs -- rule 3, §9's "coste hundido no justifica continuar".  An
      improvement that has already burned half the bonus line is exactly as
      affordable, or not, as an identical one that has burned nothing.
    * it never widens.  A caller passing `unit_costs` can make a candidate look
      cheaper, but the ceiling it is compared against is still the budget's.

    With `unit_costs=None` the conversion comes from the funding line's own
    ceiling, which is what makes the 0..1 scale mean what `estimate` says it
    means: `0.08` of the bonus line, measured against the bonus line.
    """
    pot = funding_line(line)
    if pot not in BUDGET_LINES:
        return False, "budget"
    if pot == "bonus" and budget.exhausted("core"):
        # §9: "Si core excede presupuesto, detener extras."  This is the one
        # cross-line question `afford` asks, and it is safe because it only
        # ever REFUSES: bonus is never given access to a core round, it is
        # denied a bonus one.  The rule exists because the two pots are
        # independent by construction, so without it a run that blew through
        # the errand it was given would carry on decorating -- spending a
        # budget it still nominally had on work nobody asked for, while the
        # thing that was asked for sat unfinished.
        return False, "budget"
    if unit_costs is None:
        unit_costs = {unit: budget.ceiling(pot, unit) for unit in SPENDABLE_UNITS}
    want = estimate(candidate, unit_costs=unit_costs)
    ok, _detail = budget.may_spend(pot, want)
    return (True, "") if ok else (False, "budget")


# -- booking it, and saying where it went ----------------------------------


def record(budget: CompletionBudget, line: str,
           actual: BudgetSpend) -> CompletionBudget:
    """A NEW budget with `actual` booked against the pot that funds `line`.

    The ACTUAL spend, not the estimate.  §27 asks that "el coste real actualiza
    el score de las restantes", and it can only do that if the real cost is
    what got written down: an engine that booked its own estimates would end
    every turn agreeing with itself about what it had spent.

    Rule 4 is why this goes through `funding_line`.  Booking `recovery` under
    `recovery` would leave `used("core")` untouched while `ceiling("recovery")`
    reports the core ceiling -- so the same rounds would be spendable twice,
    once under each name, and the total would be overrun without any line ever
    reporting itself exhausted.  Booked against the pot, the second request
    sees the first.
    """
    return budget.spend(funding_line(line), actual)


def exhausted_lines(budget: CompletionBudget) -> Tuple[str, ...]:
    """Every line with no room left, in `BUDGET_LINES` order.

    Resolved through `funding_line`, which is the half the contract cannot do
    for itself: `exhausted("exploration")` asks about a meter nothing is ever
    booked to, so it answers "room left" for a run whose bonus pot is dry.
    Here `exploration` is exhausted exactly when `bonus` is, because it spends
    bonus money.

    A line whose ceiling is zero is NOT exhausted, and the contract's own
    docstring explains the cost of getting that backwards: `literal` has
    `bonus_share = 0.0`, so a bonus line reported as spent would make
    `CompletionDecision.parse` refuse `core_only` -- the one honest stop a
    literal run has -- on the grounds that it ran out of a budget it was never
    given.  That test lives in `CompletionBudget.exhausted`; this function
    inherits it by asking that method rather than recomputing the condition.
    """
    out = []
    for line in BUDGET_LINES:
        if budget.exhausted(funding_line(line)):
            out.append(line)
    return tuple(out)


def report(budget: CompletionBudget) -> Dict[str, Any]:
    """A JSON-safe view of the budget, for the interface and for the closeout.

    Reports SPENDS and CEILINGS side by side and never a bare remainder.  The
    council's `BudgetState` gives the reason -- "a remainder is only meaningful
    next to the limit it came from, and two numbers that must be read together
    end up read apart" -- so `remaining` appears only in the same object as the
    `ceiling` and the `used` it was derived from.

    `funded_by` is printed for every line rather than only for the two aliases.
    A reader who sees `core` and `recovery` with identical numbers and no note
    would reasonably conclude the run had two pots; the field says which lines
    are views of which pot, which is the fact that explains the identity.

    `aggregate` sums only the lines that own a pot, so it is comparable with
    `total` -- summing all five would double-count the two aliases and report a
    turn as 185% spent.
    """
    lines: Dict[str, Any] = {}
    for line in BUDGET_LINES:
        pot = funding_line(line)
        units: Dict[str, Any] = {}
        for unit in SPENDABLE_UNITS:
            units[unit] = {
                "ceiling": budget.ceiling(pot, unit),
                "used": budget.used(pot, unit),
                "remaining": budget.remaining(pot, unit),
            }
        lines[line] = {
            "funded_by": pot,
            "alias": pot != line,
            "units": units,
            "exhausted": list(budget.exhausted(pot)),
        }
    aggregate = {unit: round(sum(budget.used(pot, unit) for pot in FUNDED_LINES), 3)
                 for unit in SPENDABLE_UNITS}
    spent = exhausted_lines(budget)
    return {
        "total": budget.total.to_dict(),
        "reserve_share": budget.reserve_share,
        "bonus_share": budget.bonus_share,
        "lines": lines,
        "funded_lines": list(FUNDED_LINES),
        "aggregate_used": aggregate,
        "exhausted_lines": list(spent),
        "any_exhausted": bool(spent),
    }
