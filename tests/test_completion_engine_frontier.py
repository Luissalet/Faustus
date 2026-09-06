"""Frontier rules of §8, §10 and §27 ("Frontier"), fixed as rules.

The load-bearing test in this file is `test_admission_cannot_read_a_candidates_
value`, and it reads the module's syntax tree rather than observing behaviour.
The guarantee -- that a blocking decision is never reduced to a score -- is
about an ORDER, and an order is exactly the kind of property that is correct
today and silently wrong after one refactor: the behavioural version passes
just as happily when the refusal happens after scoring as before it.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from src.completion_engine import budgeting, frontier, scoring
from src.completion_engine.contracts import (
    REJECTION_REASONS,
    BudgetSpend,
    ImprovementCandidate,
    ScopeEnvelope,
)


def _env(**over) -> ScopeEnvelope:
    payload = {"goal": "fix the login", "allowed_resources": ["src"]}
    payload.update(over)
    return ScopeEnvelope.parse(payload, "scope")


def _cand(title: str, **over) -> ImprovementCandidate:
    payload = {
        "title": title,
        "layer": "bonus",
        "category": "coverage",
        "relation": "direct",
        "source": "tests",
        "expected_value": 0.8,
        "estimated_cost": 0.1,
        "risk": 0.05,
        "confidence": 0.9,
        "resources": ["src/auth.py"],
        "verification": ["pytest tests/test_auth.py"],
        # Distinct by default.  Without this every candidate in a test shares
        # the coarse fallback key -- layer:category:resources -- and the second
        # one silently disappears as a `duplicate`, which is the dedupe rule
        # working correctly and quietly ruining every other test in the file.
        "dedupe_key": title,
    }
    payload.update(over)
    return ImprovementCandidate.parse(payload, "candidate")


def _budget(mode: str = "greedy", **over):
    kw = {"max_rounds": 100, "max_tool_calls": 100, "token_budget": 100_000,
          "wall_seconds": 1000.0}
    kw.update(over)
    return budgeting.from_settings(mode=mode, **kw)


def _entry(candidate: ImprovementCandidate, utility: float = 0.5):
    return frontier.FrontierEntry(candidate=candidate, utility=utility,
                                  relation=candidate.relation, admitted=True)


# -- the order -------------------------------------------------------------


def test_admission_cannot_read_a_candidates_value():
    """`_admit` may not see `expected_value`, `confidence` or `estimated_cost`.

    §8: "no reducir decisiones blocking a un score.  Un candidato fuera de
    Scope Envelope o sin permiso queda excluido."  Excluded, not scored low --
    because a refused candidate carrying a 0.94 beside `no_permission` is an
    invitation to make an exception, and value is the one field nothing outside
    this engine can check.

    `risk` IS readable and that is deliberate: a blocking risk is itself a
    blocking decision, so it belongs at admission as a threshold rather than in
    the arithmetic as a term.
    """
    tree = ast.parse(inspect.getsource(frontier._admit))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for banned in ("expected_value", "confidence", "estimated_cost"):
        assert banned not in attrs, (
            f"frontier._admit reads {banned}; admission that can see value is "
            f"admission that can be argued with")
    assert "risk" in attrs, (
        "a blocking risk must be refused at admission, not priced in a score")

    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "scoring" not in names and "utility" not in names


def test_build_admits_before_it_scores():
    """The pipeline order, read off `build`'s own source.

    Source order plus `_admit`'s purity plus the behavioural check below is
    three independent holds on the same rule.  Any one of them alone can be
    satisfied by an implementation that gets the order wrong.
    """
    tree = ast.parse(inspect.getsource(frontier.build))
    calls: dict = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        calls.setdefault(name, []).append(node.lineno)

    assert "_admit" in calls, "build no longer calls the admission gate at all"
    scored = [line for name in ("explain_score", "clears_bar")
              for line in calls.get(name, ())]
    assert scored, "build no longer scores anything; the pipeline is broken"
    assert min(calls["_admit"]) < min(scored), (
        "build scores before it admits; a refused candidate would acquire a "
        "utility, which is the appeal §8 forbids")


def test_a_refused_candidate_carries_no_score_at_all():
    """The behavioural half: `terms` is empty and `utility` is 0.0.

    An entry refused at admission has no score because none was COMPUTED, and
    the emptiness is the record of that.  A zero with a full breakdown beside
    it would say something quite different -- that it was scored and found
    worthless -- and §23 keeps origin and decision durable precisely so those
    two can be told apart afterwards.
    """
    env = _env(protected_resources=["src/settings.py"])
    refused = _cand("touch settings", resources=["src/settings.py"],
                    expected_value=1.0, confidence=1.0)
    built = frontier.build([refused], envelope=env, budget=_budget(), mode="greedy")
    entry = built.entries[0]
    assert not entry.admitted
    assert entry.terms == {}
    assert entry.utility == 0.0
    assert entry.reason in REJECTION_REASONS


# -- §27's Frontier list ---------------------------------------------------


def test_a_blocking_risk_is_excluded_even_at_a_value_of_one():
    """§27: "riesgo blocking excluye" -- with `expected_value = 1.0`.

    The case a penalty term cannot handle.  Any risk weight small enough to
    leave ordinary risky work scoreable is a weight a 1.0 outruns, so the veto
    has to be a threshold at admission.  The test also asserts that a candidate
    just below the line is still admitted, or the "veto" would just be a very
    high bar on everything.
    """
    env = _env()
    blocking = _cand("rewrite auth architecture", expected_value=1.0,
                     confidence=1.0, risk=frontier.BLOCKING_RISK,
                     estimated_cost=0.9, reversibility="none")
    ok = _cand("tighten one check", risk=frontier.BLOCKING_RISK - 0.01)

    built = frontier.build([blocking, ok], envelope=env, budget=_budget(),
                           mode="greedy")
    verdicts = {e.candidate.title: e for e in built.entries}
    assert verdicts["rewrite auth architecture"].reason == "risk"
    assert not verdicts["rewrite auth architecture"].admitted
    assert verdicts["rewrite auth architecture"].terms == {}
    assert verdicts["tighten one check"].admitted, (
        "the blocking threshold refused a candidate below it, so it is a bar "
        "on everything rather than a veto on the dangerous")


def test_duplicates_collapse_on_the_contracts_own_key():
    """§27: "dedupe".  Two generators, one finding, one candidate.

    Deduped on `ImprovementCandidate.key`, which is the contract's structural
    key -- so two descriptions of the same missing test in different words are
    one candidate, which a title comparison could never see.
    """
    env = _env()
    a = _cand("add a regression test for the login", dedupe_key="auth:login:test")
    b = _cand("cover the login path with a test", dedupe_key="auth:login:test")
    built = frontier.build([a, b], envelope=env, budget=_budget(), mode="greedy")
    assert len(built.selected()) == 1
    reasons = {c.rejection_reason for c in built.rejected()}
    assert reasons == {"duplicate"}


def test_a_candidate_waits_for_its_dependency_and_is_not_refused_forever():
    """§27: "dependencias".  Unmet dependency defers; met dependency admits.

    `quarantined` rather than a permanent refusal, because `REJECTION_REASONS`
    has no "not yet" and a candidate filed as rejected-for-value would never be
    reconsidered once its dependency landed.
    """
    env = _env()
    first = _cand("extract the helper")
    second = _cand("use the helper everywhere", dependencies=[first.id],
                   dedupe_key="auth:helper:use")

    built = frontier.build([second], envelope=env, budget=_budget(), mode="greedy")
    assert built.entries[0].reason == "quarantined"
    assert not built.entries[0].admitted

    after = frontier.build([second], envelope=env, budget=_budget(), mode="greedy",
                           executed=[first])
    assert after.selected() and after.selected()[0].id == second.id


def test_pareto_keeps_the_cheap_and_safe_and_drops_the_dominated():
    """§27: "Pareto".  Non-dominated on value/cost/risk, all three axes.

    The property that makes it a frontier rather than a ranking: a modest,
    cheap, safe improvement is NOT dominated by a brilliant, expensive, risky
    one, because it gives nothing up.  A dominance test run on the collapsed
    utility would keep only the top score and would be sorting.
    """
    env = _env()
    good = _cand("cheap and safe", expected_value=0.6, estimated_cost=0.1, risk=0.05,
                 dedupe_key="a")
    worse = _cand("worse on every axis", expected_value=0.5, estimated_cost=0.2,
                  risk=0.10, dedupe_key="b")
    other = _cand("dearer but better", expected_value=0.9, estimated_cost=0.5,
                  risk=0.05, dedupe_key="c")

    entries = [_entry(c) for c in (good, worse, other)]
    kept = {e.candidate.title for e in frontier.pareto(entries)}
    assert "worse on every axis" not in kept
    assert {"cheap and safe", "dearer but better"} <= kept, (
        "Pareto dropped a candidate that gives nothing up; that is a ranking")

    built = frontier.build([good, worse, other], envelope=env, budget=_budget(),
                           mode="greedy")
    dominated = [c for c in built.rejected() if c.rejection_reason == "dominated"]
    assert [c.title for c in dominated] == ["worse on every axis"]


def test_a_candidate_resolved_on_the_way_past_disappears_with_that_reason():
    """§27: "candidata resuelta indirectamente desaparece", reason `resolved`.

    `resolved` is its own `REJECTION_REASONS` entry precisely so that "somebody
    else fixed it" is never filed as "it was not worth doing".  The two lead a
    reader to opposite conclusions about whether the work still needs doing.
    """
    env = _env()
    stays = _cand("still needed", dedupe_key="auth:one")
    gone = _cand("fixed by the other change", dedupe_key="auth:two")

    built = frontier.build([stays, gone], envelope=env, budget=_budget(),
                           mode="greedy")
    assert len(built.selected()) == 2

    after = frontier.recompute(built, envelope=env, budget=_budget(),
                               mode="greedy", executed=[], resolved=["auth:two"])
    by_reason = {c.title: c.rejection_reason for c in after.rejected()}
    assert by_reason.get("fixed by the other change") == "resolved"
    assert "still needed" in [c.title for c in after.selected()]


def test_the_real_cost_of_a_round_updates_what_the_rest_are_worth():
    """§27: "el coste real actualiza el score de las restantes".

    Two channels, and the test drives both.  The ACTUAL spend is booked with
    `budgeting.record`, which raises the scarcity term and lowers every
    remaining utility; and what actually RAN is passed as `executed`, which
    discounts anything it overlapped through `marginal_value`.  An engine that
    fed its own estimates back would end every turn agreeing with itself.
    """
    env = _env()
    a = _cand("first", dedupe_key="x", resources=["src/auth.py"])
    b = _cand("second", dedupe_key="y", resources=["src/auth.py"])

    budget = _budget()
    before = frontier.build([a, b], envelope=env, budget=budget, mode="greedy")
    baseline = {e.candidate.title: e.utility for e in before.entries}

    spent = budgeting.record(budget, "bonus", BudgetSpend(
        rounds=int(budget.ceiling("bonus", "rounds") * 0.9),
        tokens=int(budget.ceiling("bonus", "tokens") * 0.9)))
    after = frontier.recompute(before, envelope=env, budget=spent, mode="greedy",
                               executed=[a], resolved=[])

    surviving = {e.candidate.title: e.utility for e in after.entries
                 if e.candidate.title == "second"}
    assert surviving, "the remaining candidate vanished entirely"
    assert surviving["second"] < baseline["second"], (
        "a nearly spent budget and an overlapping executed change left the "
        "remaining candidate worth exactly as much as before")


def test_a_candidate_that_stops_clearing_the_bar_leaves_with_below_threshold():
    """§9: "detenerse cuando ninguna supera threshold", recorded as a judgement.

    `below_threshold` is a statement about value and is the right record when
    the bar is what refused it -- as against `out_of_scope`, which
    `clears_bar` returns when the MODE never opened the candidate's layer and
    no judgement about the candidate was made at all.
    """
    env = _env()
    a = _cand("first", resources=["src/auth.py"])
    # Cheaper than `first`, so it survives Pareto on cost and can only ever be
    # refused by the BAR -- which is what this test is about.  Given the same
    # cost it would come out `dominated`, and the test would pass for a reason
    # it was not written to check.
    b = _cand("second", resources=["src/auth.py"],
              expected_value=0.35, estimated_cost=0.05)

    built = frontier.build([a, b], envelope=env, budget=_budget(), mode="greedy",
                           minimum=0.2)
    assert {c.title for c in built.selected()} == {"first", "second"}

    after = frontier.recompute(built, envelope=env, budget=_budget(),
                               mode="greedy", executed=[a], resolved=[],
                               minimum=0.2)
    reasons = {c.title: c.rejection_reason for c in after.rejected()}
    assert reasons.get("second") == "below_threshold"


def test_a_mode_that_never_opens_a_layer_says_so_and_does_not_call_it_worthless():
    """`literal` refusing a bonus is `out_of_scope`, never `below_threshold`.

    A depth decision recorded as a value judgement would put an opinion in the
    record that nobody formed: the run never assessed the improvement, it was
    told not to look.
    """
    ok, why = scoring.clears_bar(_cand("a bonus", layer="bonus"), mode="literal")
    assert not ok and why == "out_of_scope"
    ok, why = scoring.clears_bar(_cand("a bonus", layer="bonus"), mode="greedy",
                                 minimum=0.0)
    assert ok and why == ""


# -- §10: batching ---------------------------------------------------------


def test_a_batch_never_mixes_permissions_or_effects():
    """§10's "no agrupar: riesgo/permisos distintos; efectos externos
    independientes".

    Enforced through `_batch_key` rather than by a check afterwards, which is
    the difference between a rule and a reminder: two candidates needing
    different authority cannot land in the same group at all, so no later code
    has to remember to split them.
    """
    plain = _cand("edit the file", dedupe_key="a")
    privileged = _cand("edit and push", dedupe_key="b",
                       required_permissions=["git_push"])
    risky = _cand("risky edit", dedupe_key="c", risk=0.7)
    irreversible = _cand("irreversible edit", dedupe_key="d", reversibility="none")

    entries = [_entry(c) for c in (plain, privileged, risky, irreversible)]
    chosen, _reason = frontier.batch(entries, budget=_budget())
    assert chosen, "batching produced nothing at all"

    perms = {tuple(sorted(c.required_permissions)) for c in chosen}
    effects = {tuple(sorted(c.required_effects)) for c in chosen}
    reversibility = {c.reversibility for c in chosen}
    bands = {int(c.risk / frontier._RISK_BAND) for c in chosen}
    assert len(perms) == 1, f"one batch, {len(perms)} permission sets: {perms}"
    assert len(effects) == 1
    assert len(reversibility) == 1
    assert len(bands) == 1, "a batch mixed risk bands"


def test_a_batch_keeps_the_ids_of_the_improvements_that_caused_it():
    """§10's closing requirement, which survives every optimisation.

    "El batching reduce coste, pero no debe ocultar qué mejora originó cada
    cambio."  A batch that returned one merged unit of work would save exactly
    the same tokens and lose the attribution permanently -- and attribution is
    what lets a reader ask, six months later, which improvement caused a
    change.
    """
    a = _cand("first", dedupe_key="a")
    b = _cand("second", dedupe_key="b")
    chosen, _reason = frontier.batch([_entry(a), _entry(b)], budget=_budget())
    assert {c.id for c in chosen} == {a.id, b.id}
    assert all(c.id and c.title for c in chosen), (
        "the batch handed back something that is not attributable to a candidate")


def test_a_batch_cut_short_by_money_says_so():
    """`reason` distinguishes "this is the batch" from "this is all we could afford".

    The same tuple, two different facts, and they lead to opposite next
    actions: one is finished and the other wants a bigger budget.
    """
    small = budgeting.from_settings(mode="greedy", max_rounds=6)
    entries = [_entry(_cand(f"c{i}", dedupe_key=f"k{i}", estimated_cost=0.5),
                      utility=1.0 - i * 0.1) for i in range(4)]
    chosen, reason = frontier.batch(entries, budget=small, line="bonus")
    assert reason == "budget"
    assert len(chosen) < len(entries)

    roomy, reason2 = frontier.batch(entries[:1], budget=_budget(), line="bonus")
    assert reason2 == "" and len(roomy) == 1


def test_the_budget_is_the_last_gate_so_unaffordable_is_never_worthless():
    """A candidate refused for money keeps `budget`, not `below_threshold`.

    The two lead to opposite next actions -- one says raise the budget, the
    other says it was not worth doing -- and the ordering in `build` is what
    keeps them apart: scoring happens first, so the terms are on the entry even
    when the money ran out.
    """
    env = _env()
    broke = budgeting.from_settings(mode="greedy", max_rounds=100)
    broke = budgeting.record(broke, "bonus", BudgetSpend(
        rounds=int(broke.ceiling("bonus", "rounds"))))

    built = frontier.build([_cand("worth doing", expected_value=0.95)],
                           envelope=env, budget=broke, mode="greedy")
    entry = built.entries[0]
    assert entry.reason == "budget"
    assert entry.terms, (
        "a candidate refused for money lost its score terms, so the record "
        "cannot show it was worth doing")
    assert entry.utility > 0


def test_every_rejection_carries_a_reason_from_the_closed_vocabulary():
    """§1.8: a rejection nobody recorded comes back every round.

    Driven over a mixed pile so that each refusal path contributes at least
    one, and asserted against `REJECTION_REASONS` so a new path cannot invent
    a word the closeout will not recognise.
    """
    env = _env(protected_resources=["src/settings.py"])
    pile = [
        _cand("protected", resources=["src/settings.py"], dedupe_key="p"),
        _cand("risky", risk=0.95, dedupe_key="r"),
        _cand("dupe", dedupe_key="same"),
        _cand("dupe again", dedupe_key="same"),
        _cand("needs a tool", required_permissions=["shell"], dedupe_key="t"),
        _cand("waiting", dependencies=["nope"], dedupe_key="w"),
    ]
    built = frontier.build(pile, envelope=env, budget=_budget(), mode="greedy")
    rejected = built.rejected()
    assert rejected, "nothing was refused; this test proves nothing"
    for candidate in rejected:
        assert candidate.status == "rejected"
        assert candidate.rejection_reason in REJECTION_REASONS
    assert {"out_of_scope", "risk", "duplicate", "no_permission",
            "quarantined"} <= {c.rejection_reason for c in rejected}


def test_the_frontier_serialises_to_something_a_closeout_can_read():
    """`to_dict` is JSON-safe and keeps both halves: what ran and what did not."""
    import json

    env = _env()
    built = frontier.build([_cand("a", dedupe_key="a"),
                            _cand("b", risk=0.95, dedupe_key="b")],
                           envelope=env, budget=_budget(), mode="greedy")
    payload = built.to_dict()
    json.dumps(payload)
    assert payload["selected"] and payload["rejected"]
    assert payload["rejected"][0]["reason"] in REJECTION_REASONS
    assert payload["generated_at"]
