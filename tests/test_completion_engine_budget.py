"""Budget rules of §9 and §27 ("Presupuesto"), fixed as rules and not snapshots.

Every test here names the guarantee it holds, and asserts the RULE rather than
a number this build happens to produce: the reserve is untouchable in every
mode rather than "15 rounds are left", the total is never exceeded rather than
"the sum is 100".  A test that pins the number fails on the day somebody tunes
a share, which is the day it should have said nothing at all.

Two frozen-code findings are recorded here as `xfail(strict=True)` rather than
patched, because `completion_engine/contracts.py` is not this module's to edit:
see `test_frozen_budget_lines_double_count_the_total` and its neighbour.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from src.agent_profiles.completion import MODE_ORDER, policy_for
from src.completion_engine import budgeting
from src.completion_engine.contracts import (
    BUDGET_LINES,
    SPENDABLE_UNITS,
    BudgetSpend,
    CompletionBudget,
    CompletionDecision,
    ImprovementCandidate,
)


def _candidate(**over) -> ImprovementCandidate:
    payload = {
        "title": "add a regression test",
        "layer": "bonus",
        "category": "coverage",
        "relation": "direct",
        "source": "tests",
        "expected_value": 0.9,
        "estimated_cost": 0.1,
        "risk": 0.05,
        "confidence": 0.9,
        "resources": ["src/auth.py"],
        "verification": ["pytest tests/test_auth.py"],
    }
    payload.update(over)
    return ImprovementCandidate.parse(payload, "candidate")


def _full_budget(mode: str) -> CompletionBudget:
    return budgeting.from_settings(mode=mode, max_rounds=100, max_tool_calls=100,
                                   token_budget=100_000, wall_seconds=1000.0)


# -- the reserve -----------------------------------------------------------


@pytest.mark.parametrize("mode", MODE_ORDER)
def test_verification_reserve_is_never_spendable_by_another_line(mode):
    """The reserve survives every other line being spent dry, in all four modes.

    §9: "nunca gastar verification reserve en bonus".  The test drains every
    pot that is not `verification` -- core and bonus, and the two aliases that
    draw on them -- and then asks whether the reserve is still whole.  Draining
    first is the point: the interesting failure is not "can bonus name the
    verification line", it is "can bonus reach the reserve once its own money
    is gone", which is when a borrowing implementation would start.
    """
    budget = _full_budget(mode)
    reserve = {unit: budget.ceiling("verification", unit) for unit in SPENDABLE_UNITS}
    assert any(v > 0 for v in reserve.values()), (
        f"{mode} has no verification reserve at all; every policy in this "
        f"repository sets requires_verification=True")

    for line in ("core", "bonus", "exploration", "recovery"):
        pot = budgeting.funding_line(line)
        drain = BudgetSpend(
            rounds=int(budget.remaining(pot, "rounds")),
            tool_calls=int(budget.remaining(pot, "tool_calls")),
            tokens=int(budget.remaining(pot, "tokens")),
            seconds=max(0.0, budget.remaining(pot, "seconds")),
        )
        budget = budgeting.record(budget, line, drain)

    for unit in SPENDABLE_UNITS:
        assert budget.remaining("verification", unit) == pytest.approx(reserve[unit]), (
            f"{mode}: spending every other line moved the verification reserve")

    want = BudgetSpend(rounds=1, tool_calls=1, tokens=1, seconds=1.0)
    for line in ("core", "bonus", "exploration", "recovery"):
        ok, _why = budget.may_spend(budgeting.funding_line(line), want)
        assert not ok, f"{mode}: {line} could still spend once its own pot was dry"


@pytest.mark.parametrize("mode", MODE_ORDER)
def test_afford_never_routes_a_bonus_request_at_the_verification_pot(mode):
    """No argument to `afford` makes an extra draw on the reserve.

    The reserve is protected by `ceiling()` rather than by a check, so the way
    it would be lost is a line resolving to `verification` for someone who did
    not ask for verification.  This walks every line name and asserts the
    resolution instead of trusting the table to stay right.
    """
    for line in BUDGET_LINES:
        pot = budgeting.funding_line(line)
        if line == "verification":
            assert pot == "verification"
        else:
            assert pot != "verification", (
                f"{mode}: line {line!r} funds itself from the verification reserve")


# -- the total -------------------------------------------------------------


def test_the_total_budget_is_never_exceeded_through_this_module():
    """Spending every line through `record` never sums past the turn's total.

    §27: "budget total no se supera".  The spend goes through every line name
    INCLUDING the two aliases, because the aliases are how a total gets
    overrun: `recovery` and `exploration` carry the ceilings of `core` and
    `bonus`, so booking them separately would buy the same rounds twice.
    """
    budget = _full_budget("greedy")
    total = budget.total
    for line in BUDGET_LINES:
        pot = budgeting.funding_line(line)
        budget = budgeting.record(budget, line, BudgetSpend(
            rounds=int(budget.remaining(pot, "rounds")),
            tool_calls=int(budget.remaining(pot, "tool_calls")),
            tokens=int(budget.remaining(pot, "tokens")),
            seconds=max(0.0, budget.remaining(pot, "seconds")),
        ))
    used = budgeting.report(budget)["aggregate_used"]
    for unit in SPENDABLE_UNITS:
        assert used[unit] <= total.get(unit) + 1e-6, (
            f"{unit}: booked {used[unit]} against a total of {total.get(unit)}")


def test_the_three_funded_lines_partition_the_total_exactly():
    """core + verification + bonus == total, for every share.

    The property the total-not-exceeded rule rests on.  Asserted for all four
    modes so that a policy whose shares stopped summing to 1.0 -- a
    `bonus_budget_share` raised past `1 - reserve_share` -- is caught here
    rather than as a mysteriously negative core ceiling later.
    """
    for mode in MODE_ORDER:
        budget = _full_budget(mode)
        for unit in SPENDABLE_UNITS:
            partitioned = sum(budget.ceiling(pot, unit)
                              for pot in budgeting.FUNDED_LINES)
            assert partitioned == pytest.approx(budget.total.get(unit)), (
                f"{mode}/{unit}: the funded lines sum to {partitioned}, "
                f"not to the total {budget.total.get(unit)}")


# -- two defects found here and since FIXED in the contract ----------------
#
# Both had one root: `ceiling()` said in prose that `recovery` shares the core
# line and `exploration` the bonus one, and then `used()` metered all five
# separately by name. `contracts.FUNDING_LINES` now resolves every line to the
# pot that funds it, in `ceiling`, `used` and `any_exhausted` alike, and these
# two stay as the guards.


def test_an_aliased_line_cannot_buy_the_same_rounds_twice():
    """`recovery` spends core's money, so core's spend has to be visible to it."""
    total = BudgetSpend(rounds=100, tool_calls=100, tokens=100, seconds=100.0)
    budget = CompletionBudget(total=total, reserve_share=0.15, bonus_share=0.35)
    budget = budget.spend("core", BudgetSpend(rounds=50))
    ok, _why = budget.may_spend("recovery", BudgetSpend(rounds=50))
    assert not ok, "recovery bought the core line's rounds a second time"


def test_an_alias_reports_exhausted_when_its_pot_is_dry():
    """The mirror image, and the one that mattered more.

    `any_exhausted` is what `CompletionDecision.parse` reads to refuse a
    dishonest `converged`. While `exhausted('exploration')` looked at a meter
    nobody wrote to, a maximalist run could spend its bonus pot dry, be told
    exploration still had room, and close as "converged".
    """
    total = BudgetSpend(rounds=100, tool_calls=0, tokens=0, seconds=0.0)
    budget = CompletionBudget(total=total, reserve_share=0.15, bonus_share=0.35)
    budget = budget.spend("bonus", BudgetSpend(rounds=35))
    assert budget.exhausted("exploration"), (
        "exploration reports room while the bonus pot that funds it is dry")


def test_exhausted_lines_reports_every_line_the_dry_pot_funds():
    """`budgeting` names all the lines a dry pot starves, not just the pot.

    The contract answers per line; this answers the question a closeout asks --
    "what can this run no longer do" -- and the answer includes the aliases,
    because "exploration is out of money" is what a reader needs to be told.
    """
    budget = budgeting.from_settings(mode="greedy", max_rounds=100)
    budget = budgeting.record(budget, "bonus", BudgetSpend(rounds=35))
    spent = budgeting.exhausted_lines(budget)
    assert "bonus" in spent and "exploration" in spent
    assert "verification" not in spent and "core" not in spent


# -- core overrun, restart, sunk cost --------------------------------------


def test_a_core_overrun_stops_the_extras():
    """§9: "Si core excede presupuesto, detener extras."

    Bonus has money of its own -- the pots are independent by construction --
    so without this rule a run that blew through the errand would carry on
    decorating while the thing that was asked for sat unfinished.  The
    assertion is that the bonus request is refused WHILE the bonus pot still
    has room, which is what makes it a rule rather than an accident of
    arithmetic.
    """
    budget = budgeting.from_settings(mode="greedy", max_rounds=100)
    candidate = _candidate(estimated_cost=0.1)
    ok, _why = budgeting.afford(budget, "bonus", candidate)
    assert ok, "the bonus line was unaffordable before core had spent anything"

    budget = budgeting.record(budget, "core", BudgetSpend(
        rounds=int(budget.ceiling("core", "rounds"))))
    assert budget.remaining("bonus", "rounds") > 0, (
        "this test proves nothing unless bonus still has room of its own")

    ok, why = budgeting.afford(budget, "bonus", candidate)
    assert not ok and why == "budget"
    for line in ("bonus", "exploration"):
        assert not budgeting.afford(budget, line, candidate)[0]
    assert budgeting.afford(budget, "verification", candidate)[0], (
        "a core overrun must not stop the run PROVING what it already did")


def test_a_restart_preserves_what_was_already_consumed():
    """§27: "restart conserva consumo".  Serialise, reload, keep spending.

    The failure this prevents is a run that crashes at 90% of its budget and
    comes back believing it has a whole one.  `to_dict`/`parse` is the only
    path a budget takes across a restart, so the round trip is the test.
    """
    budget = budgeting.from_settings(mode="greedy", max_rounds=100,
                                     token_budget=100_000)
    budget = budgeting.record(budget, "core", BudgetSpend(rounds=30, tokens=20_000))
    budget = budgeting.record(budget, "bonus", BudgetSpend(rounds=10, tokens=5_000))

    revived = CompletionBudget.parse(budget.to_dict(), "budget")
    for line in BUDGET_LINES:
        pot = budgeting.funding_line(line)
        for unit in SPENDABLE_UNITS:
            assert revived.used(pot, unit) == budget.used(pot, unit)
            assert revived.remaining(pot, unit) == budget.remaining(pot, unit)
    assert budgeting.report(revived) == budgeting.report(budget)
    assert budgeting.exhausted_lines(revived) == budgeting.exhausted_lines(budget)


def test_sunk_cost_cannot_reach_the_affordability_decision():
    """§9: "coste hundido no justifica continuar" -- fixed by reading the source.

    A behavioural test cannot hold this line.  "Does spending change the
    answer?" passes trivially today and would keep passing on the day somebody
    adds a `spent_so_far` argument and only starts failing once something
    reads it.  So this reads the syntax tree: no public function in
    `budgeting` may take a parameter that carries per-candidate history, and
    `afford` may not reach for one.

    The forward-looking twin is allowed and is a different thing:
    `scoring.utility` takes `spent_share`, the share of the TURN already gone,
    which says "room is running out, be pickier".  Sunk cost says "we have
    already put so much into this one", and no argument here can express it.
    """
    banned = ("spent_on", "already_spent", "sunk", "invested", "spent_so_far",
              "candidate_spent", "history", "consumed_by")
    tree = ast.parse(inspect.getsource(budgeting))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        names = [a.arg for a in list(args.args) + list(args.kwonlyargs)
                 + list(args.posonlyargs)]
        for name in names:
            assert not any(bad in name.lower() for bad in banned), (
                f"budgeting.{node.name} takes {name!r}: that is a channel for "
                f"sunk cost, which §9 says must never justify continuing")

    afford = ast.parse(inspect.getsource(budgeting.afford))
    attrs = {n.attr for n in ast.walk(afford) if isinstance(n, ast.Attribute)}
    assert "used" not in attrs, (
        "budgeting.afford reads `used`; affordability is about the NEXT spend, "
        "and reading what has gone is how a run talks itself into continuing")


def test_the_cost_scale_is_a_fraction_of_the_line_and_not_of_the_turn():
    """`estimated_cost = 0.08` is 8% of its LINE, measured against that line.

    The failure this pins is the one the module docstring warns about: a reader
    who takes the 0..1 as a share of the TURN bills a one-line regression test
    eight percent of the whole budget.  Two assertions hold the scale -- the
    estimate scales with the LINE's ceiling, and it is bounded by it.
    """
    budget = budgeting.from_settings(mode="greedy", max_rounds=0,
                                     token_budget=100_000)
    ceiling = budget.ceiling("bonus", "tokens")
    candidate = _candidate(estimated_cost=0.08)

    priced = budgeting.estimate(
        candidate, unit_costs={u: budget.ceiling("bonus", u) for u in SPENDABLE_UNITS})
    assert priced.tokens == pytest.approx(0.08 * ceiling, rel=0.01)
    assert priced.tokens < budget.total.tokens * 0.08, (
        "the estimate was priced against the turn's total, not the bonus line")

    whole_line = budgeting.estimate(
        _candidate(estimated_cost=1.0),
        unit_costs={u: budget.ceiling("bonus", u) for u in SPENDABLE_UNITS})
    assert whole_line.tokens == pytest.approx(ceiling, rel=0.01), (
        "a candidate costing 1.0 must cost exactly one line, by definition")


def test_a_cheap_candidate_never_rounds_down_to_free():
    """Whole units round UP, so no candidate is ever billed zero rounds.

    Rounding down would make cheap candidates free, and free candidates never
    exhaust anything: an unbounded number of them would run inside a budget
    that reported itself untouched the whole time.
    """
    spend = budgeting.estimate(_candidate(estimated_cost=0.01),
                               unit_costs={"rounds": 4.0, "tool_calls": 1.0,
                                           "tokens": 10.0, "seconds": 1.0})
    assert spend.rounds >= 1 and spend.tool_calls >= 1 and spend.tokens >= 1
    free = budgeting.estimate(_candidate(estimated_cost=0.0),
                              unit_costs={"rounds": 4.0, "tool_calls": 1.0,
                                          "tokens": 10.0, "seconds": 1.0})
    assert free.empty, "a candidate costing nothing must be billed nothing"


def test_from_settings_gives_bonus_share_the_total_as_its_denominator():
    """The share is OF THE TURN, not of what survives core.  §9 and SPENDABLE_UNITS.

    Asserted as the property that distinguishes the two readings: the bonus
    ceiling does not move when core spends.  Under the remainder reading a run
    that overspent on core would get a SMALLER bonus and one that underspent a
    bigger one -- which sounds reasonable until it is stated the other way
    round, as the incentive it actually creates.
    """
    for mode in MODE_ORDER:
        budget = budgeting.from_settings(mode=mode, max_rounds=100)
        share = policy_for(mode).bonus_budget_share
        before = budget.ceiling("bonus", "rounds")
        assert before == pytest.approx(share * 100), (
            f"{mode}: bonus ceiling {before} is not {share} of the turn's 100")
        after = budgeting.record(budget, "core", BudgetSpend(rounds=40))
        assert after.ceiling("bonus", "rounds") == pytest.approx(before), (
            f"{mode}: spending core moved the bonus ceiling, so the share is "
            f"being taken of the remainder rather than of the total")


# -- the rule the subsystem is named for -----------------------------------


def _drained(mode: str = "greedy") -> CompletionBudget:
    """A budget with every pot spent to its ceiling."""
    budget = budgeting.from_settings(mode=mode, max_rounds=100)
    for pot in budgeting.FUNDED_LINES:
        budget = budgeting.record(budget, pot, BudgetSpend(
            rounds=int(budget.remaining(pot, "rounds"))))
    return budget


def test_converged_is_never_returned_while_a_budget_line_is_exhausted():
    """Direction one, and the whole point of the module.

    "We ran out" dressed as "there was nothing left worth doing" is the one
    sentence that stops anybody from raising the budget: convergence invites no
    action and exhaustion invites exactly one.  Asserted over every settled
    state a run can reach, against a drained budget, in all four modes -- and
    for BOTH honest stops, since `CompletionDecision.parse` refuses `core_only`
    on the same grounds.
    """
    from src.completion_engine import convergence
    from src.completion_engine.contracts import HONEST_STOPS

    for mode in MODE_ORDER:
        budget = _drained(mode)
        assert budgeting.exhausted_lines(budget), "this budget is not drained"
        for reason in ("empty", "stalled", "max_rounds", ""):
            state = convergence.ConvergenceState(rounds=3, executed=2,
                                                 stalled_rounds=2, reason=reason)
            got = convergence.stop_reason(state=state, budget=budget)
            assert got not in HONEST_STOPS, (
                f"{mode}/{reason}: reported {got!r} while "
                f"{budgeting.exhausted_lines(budget)} were exhausted")
            assert got == "budget"


def test_budget_is_never_returned_while_nothing_has_run_out():
    """Direction two: the reverse lie, which sends somebody to raise a fine limit."""
    from src.completion_engine import convergence

    budget = budgeting.from_settings(mode="greedy", max_rounds=100)
    assert not budgeting.exhausted_lines(budget)
    for reason in ("", "empty", "stalled"):
        state = convergence.ConvergenceState(rounds=1, executed=1, reason=reason)
        got = convergence.stop_reason(state=state, budget=budget)
        assert got != "budget", (
            f"{reason!r} reported a budget stop with every line untouched")

    hit = convergence.ConvergenceState(rounds=100, reason="max_rounds")
    assert convergence.stop_reason(state=hit, budget=budget) == "budget", (
        "a run that used its last round DID run out; rounds are a budget unit")


def test_the_decision_contract_accepts_every_stop_this_module_produces():
    """The end-to-end version: `CompletionDecision.parse` must never refuse us.

    Rule 2 is enforced from the other side by `parse`, at the close of the run,
    on the object about to be persisted -- the most expensive place to find
    out.  This drives both modules together so that the refusal is proved
    unreachable rather than merely unlikely.
    """
    from src.completion_engine import convergence

    for budget in (budgeting.from_settings(mode="greedy", max_rounds=100),
                   _drained("greedy"), _drained("literal")):
        for reason in ("", "empty", "stalled", "max_rounds"):
            for cancelled, blocked in ((False, False), (True, False), (False, True)):
                state = convergence.ConvergenceState(rounds=2, reason=reason)
                stop = convergence.stop_reason(state=state, budget=budget,
                                               cancelled=cancelled, blocked=blocked)
                if not stop:
                    continue
                decision = CompletionDecision.parse(
                    {"stop_reason": stop, "budget": budget.to_dict()}, "decision")
                assert decision.stop_reason == stop


def test_a_literal_run_may_still_stop_honestly_with_an_unopened_bonus_line():
    """`literal` has `bonus_share = 0.0`, and a line never opened is not spent.

    The contract's own warning: a zero-ceiling bonus reported as exhausted
    would make `parse` refuse `core_only` -- the one honest stop a literal run
    has -- on the grounds that it ran out of a budget it was never given.  Ran
    out and never started are different facts.
    """
    from src.completion_engine import convergence

    budget = budgeting.from_settings(mode="literal", max_rounds=100)
    assert budget.ceiling("bonus", "rounds") == 0
    assert "bonus" not in budgeting.exhausted_lines(budget)
    state = convergence.ConvergenceState(rounds=1, reason="empty")
    assert convergence.stop_reason(state=state, budget=budget) == "converged"
    CompletionDecision.parse({"stop_reason": "core_only",
                              "budget": budget.to_dict()}, "decision")
