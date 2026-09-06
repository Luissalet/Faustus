"""The closing report, and the adapters that feed it when nothing is switched on.

The claims this file exists to keep true:

1. Core and extras are SEPARATE, and the extras line is printed even when it is
   empty. §30: an absent line reads as "there were none" and an empty one reads
   as "we checked", and only one of those is a statement anybody made.
2. A run with no extras says nothing that suggests it had any.
3. "Not done" says WHY, in the vocabulary of `REJECTION_REASONS`.
4. A stop for budget and a stop for convergence produce different sentences.
   The contract already refuses the combination in `parse`; this is about the
   renderer, which could still print two exhausted lines under wording that
   reads like "there was nothing left worth doing".
5. `degraded_integrations` reaches the report. A close that could not consult
   the Delta Engine did not see scope creep and must not claim otherwise.
6. With both optional subsystems off the adapters answer empty, `degraded()`
   names them, and NOTHING raises -- this code runs on the turn path.
7. `judge_turn` over a real `TurnLedger.summary()` produces a verdict from
   `prove.VERDICTS`, through `changesets` rather than around it.
"""

from __future__ import annotations

import pytest

from src import prove
from src.completion_engine import adapters, closeout
from src.completion_engine.adapters import delta_engine as delta_adapter
from src.completion_engine.adapters import proof as proof_adapter
from src.completion_engine.adapters import state_mirror as state_adapter
from src.completion_engine.contracts import (
    REJECTION_REASONS,
    STOP_REASONS,
    CompletionContract,
    CompletionDecision,
    CompletionError,
)


def _executed(title: str, layer: str) -> dict:
    return {"title": title, "layer": layer, "status": "done",
            "evidence_refs": [f"evidence:{title}"]}


def _rejected(title: str, reason: str) -> dict:
    return {"title": title, "layer": "bonus", "status": "rejected",
            "rejection_reason": reason}


def _decision(**overrides) -> CompletionDecision:
    payload = {
        "mode": "greedy",
        "completed_layers": ["core", "professional"],
        "stop_reason": "converged",
        "executed": [
            _executed("fix the expired-token path in src/auth.py", "core"),
            _executed("add a regression test for the expired token", "professional"),
            _executed("fix the same pattern in provider B", "bonus"),
            _executed("improve the login failure diagnostic", "bonus"),
        ],
        "rejected": [_rejected("refactor the whole auth package", "out_of_scope"),
                     _rejected("deploy the change", "forbidden_effect")],
        "run_id": "run_1",
    }
    payload.update(overrides)
    return CompletionDecision.parse(payload, "decision")


def _budget(spent_rounds: int) -> dict:
    """A budget whose core pot is exhausted at `spent_rounds`.

    Ten rounds total, 15% reserved for verification and 35% for bonus, leaves
    five for the core pot -- so ten spent on `core` empties it, which is what
    `CompletionDecision.parse` reads to refuse a dishonest `converged`.
    """
    return {"total": {"rounds": 10}, "reserve_share": 0.15, "bonus_share": 0.35,
            "spent": {"core": {"rounds": spent_rounds}}}


# -- 1 & 2. core and extras are separate, always ---------------------------

def test_the_extras_come_from_the_contracts_own_method():
    """`decision.extras()` is `bonus` + `exploratory`. Recomputing that here
    would be a second definition of "unrequested" in the one report whose job
    is to keep the two apart."""
    decision = _decision()
    report = closeout.build(decision)
    assert report.extras == tuple(c.title for c in decision.extras())
    assert set(report.core).isdisjoint(report.extras)


def test_the_core_line_holds_only_the_required_layers():
    report = closeout.build(_decision())
    assert "fix the expired-token path in src/auth.py" in report.core
    assert "add a regression test for the expired token" in report.core
    assert "fix the same pattern in provider B" not in report.core


def test_no_extra_is_hidden_inside_the_core_summary():
    """§30, read off the rendered text and not off the dataclass: whatever the
    fields say, the CORE LINE must not contain an extra's title."""
    report = closeout.build(_decision())
    core_line = [ln for ln in closeout.render(report).splitlines()
                 if ln.startswith("Core:")][0]
    for extra in report.extras:
        assert extra not in core_line


def test_the_extras_line_is_printed_even_when_there_are_none():
    decision = _decision(executed=[_executed("fix the bug", "core")])
    text = closeout.render(closeout.build(decision))
    assert "Extras: none." in text
    lines = [ln for ln in text.splitlines() if ln.startswith("Extras:")]
    assert len(lines) == 1, "exactly one extras line, always"


def test_a_run_with_no_extras_says_nothing_that_suggests_it_had_any():
    """The other half of the rule: `Extras: none` must be the ONLY thing the
    report says about extras, with no count, no "also", no hedge."""
    decision = _decision(executed=[_executed("fix the bug", "core")], rejected=[])
    report = closeout.build(decision)
    assert report.extras == ()
    text = closeout.render(report)
    assert "Extras: none." in text
    assert text.count("Extras") == 1
    for weasel in ("also", "additionally", "as well", "plus"):
        assert weasel not in text.lower()


# -- 3. "not done" says why -------------------------------------------------

def test_every_rejection_reason_has_a_sentence():
    """A reason with no phrase would print as a bare token on the one line
    whose whole job is to explain itself."""
    assert closeout.unphrased() == {"rejection_reasons": (), "stop_reasons": ()}
    assert set(closeout.REASON_PHRASES) == set(REJECTION_REASONS)
    assert set(closeout.STOP_PHRASES) == set(STOP_REASONS)


def test_not_done_carries_the_reason_for_every_entry():
    report = closeout.build(_decision())
    assert len(report.not_done) == 2
    for entry in report.not_done:
        assert "(" in entry and entry.endswith(")")
    joined = " ".join(report.not_done)
    assert closeout.REASON_PHRASES["out_of_scope"] in joined
    assert closeout.REASON_PHRASES["forbidden_effect"] in joined


def test_a_deferred_candidate_without_a_reason_still_says_something():
    """`parse` allows a deferred row with no reason -- deferred already means
    "worth doing, not now" -- so the report supplies the sentence."""
    decision = _decision(rejected=[], deferred=[
        {"title": "extract the retry helper", "layer": "bonus",
         "status": "deferred"}])
    report = closeout.build(decision)
    assert report.not_done == (
        f"extract the retry helper ({closeout.DEFERRED_PHRASE})",)


def test_nothing_refused_says_so_rather_than_printing_an_empty_line():
    decision = _decision(rejected=[])
    text = closeout.render(closeout.build(decision))
    assert "Not done: nothing was refused or deferred." in text


# -- 4. running out is never written as converging --------------------------

def _stop_line(decision) -> str:
    text = closeout.render(closeout.build(decision))
    return [ln for ln in text.splitlines() if ln.startswith("Stop reason:")][0]


def test_the_contract_refuses_a_converged_stop_on_an_exhausted_budget():
    """The guarantee the renderer leans on. If this stops holding, every
    assertion below about the two sentences becomes decorative."""
    with pytest.raises(CompletionError) as raised:
        CompletionDecision.parse({"stop_reason": "converged",
                                  "budget": _budget(10)}, "decision")
    assert "converge" in str(raised.value)


def test_a_budget_stop_and_a_convergence_stop_are_different_sentences():
    ran_out = _stop_line(_decision(stop_reason="budget", budget=_budget(10)))
    converged = _stop_line(_decision(stop_reason="converged"))

    assert ran_out != converged
    assert closeout.STOP_PHRASES["converged"] not in ran_out
    assert closeout.STOP_PHRASES["budget"] not in converged
    # The two sentences must not merely differ -- they must lead to different
    # next actions, which is the whole point of keeping them apart.
    assert "marginal value bar" in converged and "marginal value bar" not in ran_out
    assert "raise it" in ran_out and "raise it" not in converged


def test_a_budget_stop_names_the_lines_that_ran_out():
    """"A budget line ran out" without saying which one is a sentence the
    reader cannot act on, and acting on it is why this is not convergence."""
    line = _stop_line(_decision(stop_reason="budget", budget=_budget(10)))
    assert "exhausted:" in line
    assert "core" in line.split("exhausted:", 1)[1]


def test_every_stop_reason_renders_a_sentence_of_its_own():
    seen = {}
    for reason in STOP_REASONS:
        payload = {"stop_reason": reason, "run_id": "run_1"}
        if reason == "budget":
            payload["budget"] = _budget(10)
        line = _stop_line(CompletionDecision.parse(payload, "decision"))
        assert line not in seen.values(), \
            f"{reason} renders the same sentence as {seen}"
        seen[reason] = line


def test_the_stop_detail_is_carried_into_the_line():
    decision = _decision(stop_reason="blocked",
                         stop_detail="the migration has to land first")
    assert "the migration has to land first" in _stop_line(decision)


# -- 5. a dark integration is disclosed ------------------------------------

def test_degraded_integrations_reach_the_report():
    decision = _decision(degraded_integrations=["delta_engine", "state_mirror"])
    report = closeout.build(decision)
    text = closeout.render(report)
    assert "Not consulted: delta_engine, state_mirror" in text
    assert "delta_engine" in report.to_dict()["degraded_integrations"]
    assert "degraded delta_engine+state_mirror" in closeout.summary_line(report)


def test_a_healthy_run_does_not_print_a_warning_shaped_line():
    """An empty extras list is a finding; an empty degraded list is the absence
    of a caveat, and "Not consulted: none" on every healthy run is how a
    warning stops being read."""
    text = closeout.render(closeout.build(_decision()))
    assert "Not consulted" not in text


def test_the_report_has_section_32s_five_lines_in_order():
    text = closeout.render(closeout.build(_decision()))
    heads = [ln.split(":", 1)[0] for ln in text.splitlines()]
    assert heads == ["Core", "Extras", "Not done", "Proof", "Stop reason"]


# -- what the delta engine adds when it IS on -------------------------------

_DELTA = {
    "assertions": [
        {"id": "a1", "path": "src/session.py", "classification": "regression",
         "operation": "modified"},
        {"id": "a2", "path": "src/auth.py", "classification": "incidental",
         "operation": "modified"},
        {"id": "a3", "path": "src/auth.py", "classification": "requested",
         "operation": "modified"},
    ],
    "out_of_scope": ["static/js/app.js"],
}


def test_an_incidental_change_is_an_extra_and_scope_creep_is_labelled():
    report = closeout.build(_decision(), delta=_DELTA)
    assert "src/auth.py (an unasked change the delta engine saw)" in report.extras
    assert "static/js/app.js (outside the intent's scope)" in report.extras


def test_an_unrepaired_regression_lands_in_not_done_with_its_own_phrase():
    """Not one of `REASON_PHRASES`: nobody refused it, it was observed and
    left, and printing it under a rejection reason would invent a decision."""
    report = closeout.build(_decision(), delta=_DELTA)
    entry = [e for e in report.not_done if "src/session.py" in e][0]
    assert closeout.OBSERVED_REGRESSION_PHRASE in entry
    assert closeout.OBSERVED_REGRESSION_PHRASE not in closeout.REASON_PHRASES.values()


def test_a_requested_change_is_not_reported_as_an_extra():
    report = closeout.build(_decision(), delta=_DELTA)
    assert not [e for e in report.extras if "requested" in e]


# -- the contract's promise -------------------------------------------------

def test_a_run_that_executed_nothing_names_the_promise_it_did_not_meet():
    contract = CompletionContract.parse(
        {"mode": "greedy", "core_deliverables": ["reject an expired token",
                                                 "log the refusal"]},
        "completion_contract")
    decision = _decision(executed=[], rejected=[], stop_reason="failed")
    report = closeout.build(decision, contract=contract)
    assert any("2 core deliverable(s)" in e for e in report.not_done)
    assert "nothing was recorded as executed" in closeout.render(report)


def test_the_promise_is_never_printed_as_though_it_had_been_done():
    """The inversion this module exists to prevent: the core line holds
    results, never the contract's wishes."""
    contract = CompletionContract.parse(
        {"mode": "greedy", "core_deliverables": ["reject an expired token"]},
        "completion_contract")
    report = closeout.build(_decision(executed=[], rejected=[],
                                      stop_reason="failed"), contract=contract)
    assert report.core == ()
    core_line = [ln for ln in closeout.render(report).splitlines()
                 if ln.startswith("Core:")][0]
    assert "reject an expired token" not in core_line


def test_a_run_that_did_core_work_makes_no_claim_about_which_promise_it_met():
    """Matching a deliverable's prose to a candidate's title would be a guess.
    The report says nothing rather than guessing in either direction."""
    contract = CompletionContract.parse(
        {"mode": "greedy", "core_deliverables": ["reject an expired token"]},
        "completion_contract")
    report = closeout.build(_decision(), contract=contract)
    assert not [e for e in report.not_done if "core deliverable(s)" in e]


# -- 6. the adapters, with everything switched off --------------------------

@pytest.fixture
def all_off(monkeypatch):
    """Both optional subsystems off, at the setting the real `enabled()` reads.

    Patched at `src.settings.get_setting` rather than at each adapter, so the
    whole delegation chain is exercised: `adapters.delta_engine.enabled` ->
    `delta_engine.service.enabled` -> the setting. Patching the adapters
    themselves would test the mocks.
    """
    import src.settings as settings_mod

    real = settings_mod.get_setting

    def fake(key, default=None, *args, **kwargs):
        if key in ("agent_delta_engine", "agent_state_mirror"):
            return False
        return real(key, default, *args, **kwargs)

    monkeypatch.setattr(settings_mod, "get_setting", fake)
    return fake


def test_with_both_subsystems_off_the_switches_say_so(all_off):
    assert delta_adapter.enabled() is False
    assert state_adapter.enabled() is False
    ready = adapters.available()
    assert ready["delta_engine"] is False and ready["state_mirror"] is False
    assert set(ready) == set(adapters.INTEGRATIONS)


def test_degraded_names_the_subsystems_that_are_off(all_off):
    dark = adapters.degraded()
    assert "delta_engine" in dark and "state_mirror" in dark
    assert set(dark) <= set(adapters.INTEGRATIONS)
    positions = [adapters.INTEGRATIONS.index(name) for name in dark]
    assert positions == sorted(positions), \
        "the order is INTEGRATIONS' order, so a receipt reads the same twice"


def test_the_delta_adapter_answers_empty_and_never_raises(all_off):
    assert delta_adapter.delta_for_turn(
        owner="alice", workspace="/tmp/ws",
        source={"kind": "checkpoint", "ref": "aaa"},
        target={"kind": "checkpoint", "ref": "bbb"}) is None
    assert delta_adapter.scope_creep(None) == ()
    assert delta_adapter.regressions(None) == ()
    assert delta_adapter.incidental(None) == ()


def test_the_state_adapter_answers_empty_and_never_raises(all_off):
    assert state_adapter.situations(owner="alice") == {}
    assert state_adapter.unfinished(owner="alice") == ()


def test_a_missing_owner_is_not_an_exception_either(all_off):
    """Every service method here requires an owner and raises without one.
    This code is on the turn path: a missing owner must cost an empty answer,
    never a turn."""
    assert state_adapter.situations(owner="") == {}
    assert delta_adapter.delta_for_turn(owner="", workspace="", source={},
                                        target={}) is None


@pytest.mark.parametrize("garbage", [None, {}, "not a delta", 42,
                                     {"assertions": "nope"},
                                     {"assertions": [None, 7, {"x": 1}]},
                                     {"out_of_scope": "a string"}])
def test_the_delta_readers_survive_anything(garbage):
    """These come off a stored JSON blob. A malformed row is dropped, never
    coerced into a nameless entry on a receipt."""
    assert delta_adapter.scope_creep(garbage) == ()
    assert delta_adapter.regressions(garbage) == ()
    assert delta_adapter.incidental(garbage) == ()


def test_the_delta_readers_use_discoverys_own_vocabulary():
    """`scope_creep` reads `out_of_scope`, which is `classification.Classified`'s
    third field and the key `discovery._delta_rows` reads. `CLASSIFICATIONS` has
    no member of that name, so a reader that invented an assertion class for it
    would answer () on every real delta and nobody would notice."""
    from src.delta_engine.contracts import CLASSIFICATIONS

    assert "out_of_scope" not in CLASSIFICATIONS
    assert "regression" in CLASSIFICATIONS and "incidental" in CLASSIFICATIONS
    assert delta_adapter.scope_creep(_DELTA) == ("static/js/app.js",)
    assert [a["id"] for a in delta_adapter.regressions(_DELTA)] == ["a1"]
    assert [a["id"] for a in delta_adapter.incidental(_DELTA)] == ["a2"]


def test_the_state_adapter_only_asks_for_real_situations():
    from src.state_mirror.queries import SITUATIONS

    for name in state_adapter.UNFINISHED_SITUATIONS:
        assert name in SITUATIONS


def test_a_closeout_built_with_everything_off_discloses_it(all_off):
    """The whole point of `degraded()`: it becomes the sentence the reader sees."""
    decision = _decision(degraded_integrations=list(adapters.degraded()))
    text = closeout.render(closeout.build(decision))
    assert "Not consulted:" in text
    assert "delta_engine" in text and "state_mirror" in text
    assert "does not claim what" in text


def test_a_single_dark_integration_is_named_in_the_singular():
    """One integration is "it", not "them". A receipt that reads as though it
    were written by a machine is a receipt people skim past."""
    text = closeout.render(closeout.build(
        _decision(degraded_integrations=["state_mirror"])))
    assert "written without it and does not claim what it would have seen" in text
    plural = closeout.render(closeout.build(
        _decision(degraded_integrations=["state_mirror", "delta_engine"])))
    assert "written without them and does not claim what they would have seen" \
        in plural


# -- 7. the proof adapter goes through changesets ---------------------------

_TURN = {
    "language": "en",
    "mutations": ["src/auth.py"],
    "checkpoint": "0" * 40,
    "tool_calls": 6,
    "tests": {"ran": True, "ok": True, "command": "pytest -q",
              "summary": "12 passed", "failures": []},
    "review": {"verdict": "ok", "findings": []},
}


def test_judge_turn_produces_a_verdict_from_proves_own_vocabulary():
    proof = proof_adapter.judge_turn(_TURN, workspace="")
    assert proof_adapter.verdict_of(proof) in prove.VERDICTS
    assert proof.get("verdict") in prove.VERDICTS
    assert "confidence" in proof


def test_judge_turn_goes_through_changesets_and_not_around_prove():
    """`changesets.judge` folds `ChangeSet.evidence_gaps()` in before calling
    `prove`. A caller that went straight to `prove.prove` would print a
    verdict that sounds surer than the change card for the same turn."""
    import inspect as _inspect

    source = _inspect.getsource(proof_adapter.judge_turn)
    assert "changesets.from_turn" in source and "changesets.judge" in source
    assert "prove.prove" not in source


def test_an_unusable_summary_answers_an_empty_packet_rather_than_raising():
    assert proof_adapter.judge_turn({"tests": "not a mapping"}) == {}
    assert proof_adapter.verdict_of({}) == ""
    assert proof_adapter.is_proved({}) is False
    assert proof_adapter.uncertainty_line({}) == ""


def test_an_unrecognised_verdict_reads_as_not_established():
    assert proof_adapter.verdict_of({"verdict": "probably_fine"}) == ""
    assert proof_adapter.verdict_of(None) == ""


def test_partial_is_not_proved():
    """§19: the close distinguishes a proved core from a partial bonus, so the
    two must not collapse into one word on a receipt."""
    assert "partial" in prove.VERDICTS
    assert proof_adapter.is_proved({"verdict": "partial"}) is False
    assert proof_adapter.is_proved({"verdict": "proved"}) is True


def test_the_proof_line_names_the_doubt_for_anything_short_of_proved():
    proof = {"verdict": "unproved", "confidence": 0.2,
             "uncertainty": [{"kind": "no_verification_runner",
                              "detail": "nothing ran that could prove the work"}]}
    report = closeout.build(_decision(), proof=proof)
    assert report.proof.startswith("unproved — no_verification_runner:")
    assert "Proof: unproved — no_verification_runner:" in closeout.render(report)


def test_a_proved_verdict_prints_alone():
    report = closeout.build(_decision(), proof={"verdict": "proved",
                                                "confidence": 1.0})
    assert report.proof == "proved"
    assert "Proof: proved." in closeout.render(report)


def test_no_proof_at_all_is_not_the_same_as_unproved():
    """`unproved` is a verdict somebody computed; "not established" is the
    absence of one, and the report must not print the first for the second."""
    report = closeout.build(_decision())
    assert report.proof.startswith("not established")
    assert "unproved" not in report.proof


def test_a_run_that_points_at_a_proof_it_does_not_carry_says_so():
    report = closeout.build(_decision(proof_refs=["proof_abc"]))
    assert "proof reference(s)" in report.proof


# -- the report as data -----------------------------------------------------

def test_to_dict_reuses_the_decisions_own_summary():
    report = closeout.build(_decision())
    payload = report.to_dict()
    assert payload["decision"] == report.decision.summary()
    assert payload["rendered"] == closeout.render(report)
    assert payload["honest_stop"] is True
    assert payload["extras"] == list(report.extras)


def test_summary_line_counts_rather_than_quotes():
    report = closeout.build(_decision())
    line = closeout.summary_line(report)
    assert "greedy" in line and "2 core" in line and "2 extra(s)" in line
    assert "stop converged" in line
    for title in report.core + report.extras:
        assert title not in line


def test_build_accepts_the_mapping_a_decision_serialises_to():
    """And therefore inherits `parse`'s refusal: a hand-built dict claiming
    convergence on an exhausted budget cannot be rendered into a clean report."""
    decision = _decision()
    assert closeout.build(decision.to_dict()).core == closeout.build(decision).core
    with pytest.raises(CompletionError):
        closeout.build({"stop_reason": "converged", "budget": _budget(10)})
