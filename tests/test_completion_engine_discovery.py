"""Where improvements come from: evidence, determinism, and no model.

§7 and §9, pinned. The claims this file exists to keep true:

1. Every generator produces candidates WITH evidence, and the contract refuses
   a `core` or `professional` candidate without one -- so a layer that can
   block the close can never rest on nobody's evidence.
2. No generator calls a model. Checked by reading `discovery.py`'s own syntax
   tree, not by observing a run: a run can be clean on the day a model call is
   added behind a branch nothing exercised.
3. `dedupe` resolves a collision by SOURCE TRUST, not by arrival order, and
   keeps the union of the evidence. Keeping the first arrival would make the
   answer depend on the order this file happens to register its generators.
4. `resolved_by` retires a candidate whose problem is gone -- §9's "eliminar
   las resueltas indirectamente", which is the only thing stopping the frontier
   from growing forever.
5. An absent report produces nothing. "The test runner did not report" and
   "the test runner reported that it did not run" are different facts.
"""

from __future__ import annotations

import ast
import inspect
import sys
import types

import pytest

from src.completion_engine import discovery, scope
from src.completion_engine.contracts import (
    DISCOVERY_SOURCES,
    REQUIRED_LAYERS,
    CompletionContract,
    CompletionError,
    ImprovementCandidate,
)


def _contract(**overrides) -> CompletionContract:
    payload = {
        "mode": "greedy",
        "definition_of_done": ["src/auth.py rejects an expired token",
                               "the README explains the new flag"],
        "professional_expectations": ["docs/api.md matches the new signature"],
        "run_id": "run_1",
    }
    payload.update(overrides)
    return CompletionContract.parse(payload, "completion_contract")


def _envelope():
    return scope.compile_envelope(goal="fix the expired-token bug in src/auth.py",
                                  mode="greedy", touched=["src/auth.py"])


def _rich_input(**overrides) -> discovery.DiscoveryInput:
    """One turn with something for every deterministic source to find."""
    kwargs = dict(
        contract=_contract(),
        envelope=_envelope(),
        ledger_summary={"mutations": ["src/auth.py"]},
        static_checks={"ran": True, "inconclusive": False, "findings": [
            {"path": "src/auth.py", "line": 42, "code": "F821",
             "msg": "undefined name 'expiry'", "tool": "ruff"}]},
        tests={"ran": True, "ok": False, "label": "pytest",
               "failures": ["tests/test_auth.py::test_expiry - AssertionError"],
               "related_files": []},
        changeset={"verdict": "unproved",
                   "unsupported_claims": [{"path": "src/session.py",
                                           "claimed": "modified",
                                           "reason": "nothing changed at that path"}],
                   "unclaimed_changes": ["src/auth.py"]},
        proof={"verdict": "unproved", "confidence": 0.3,
               "uncertainty": [{"kind": "no_verification_runner",
                                "detail": "nothing ran that could prove the work"}]},
        delta={"assertions": [
            {"id": "a1", "path": "src/auth.py", "classification": "regression",
             "operation": "modified", "severity": "material"},
            {"id": "a2", "path": "src/auth.py", "classification": "incidental",
             "operation": "modified"}],
            "unmet": ["an expired token is rejected"],
            "out_of_scope": ["static/js/app.js"]},
        situations={
            "unverified_changes": [{"entity_id": "run_1", "field": "status",
                                    "reason": "reported", "refresh": "run prove"}],
            "blocked_work": [{"entity_id": "obj_9", "reason": "blocked_by",
                              "blocked_by": ["obj_8"]}]},
    )
    kwargs.update(overrides)
    return discovery.DiscoveryInput(**kwargs)


# -- evidence ---------------------------------------------------------------

def test_the_contract_refuses_a_required_layer_with_no_evidence():
    """The guarantee the generators lean on. If this ever stops holding, every
    "with evidence" assertion below becomes decorative."""
    for layer in REQUIRED_LAYERS:
        with pytest.raises(CompletionError) as raised:
            ImprovementCandidate.parse({"title": "an opinion", "layer": layer},
                                       "candidate")
        assert "evidence_refs" in str(raised.value)


def test_every_generator_produces_candidates_with_evidence():
    data = _rich_input()
    produced = 0
    for generator in discovery.GENERATORS:
        for candidate in generator(data):
            produced += 1
            assert candidate.evidence_refs, \
                f"{generator.__name__} produced {candidate.title!r} with no evidence"
            assert candidate.source in DISCOVERY_SOURCES
    assert produced >= 8


def test_the_registry_has_one_generator_per_deterministic_source():
    covered = {c.source for c in discovery.discover(_rich_input())}
    for source in ("definition_of_done", "static_check", "tests", "delta",
                   "proof", "state_mirror", "changeset"):
        assert source in covered, f"{source} produced nothing on a rich turn"


def test_nothing_here_prices_its_own_proposals():
    """§30: a generator that proposed AND scored would be the only voice on the
    value of what it proposed. The frontier fills these in from elsewhere."""
    for candidate in discovery.discover(_rich_input()):
        assert candidate.expected_value == 0.0
        assert candidate.estimated_cost == 0.0
        assert candidate.risk == 0.0


# -- the individual sources -------------------------------------------------

def test_a_definition_of_done_line_the_ledger_covers_is_not_proposed():
    found = discovery.discover(_rich_input(), sources=["definition_of_done"])
    titles = " | ".join(c.title for c in found)
    assert "README" in titles                    # nothing showed this one done
    assert "expired token" not in titles         # src/auth.py was mutated
    assert all(c.layer == "core" for c in found)


def test_a_static_finding_is_professional_and_names_its_tool_and_line():
    found = discovery.discover(_rich_input(), sources=["static_check"])
    assert len(found) == 1
    assert found[0].layer == "professional"
    assert found[0].category == "correctness"
    assert "ruff:src/auth.py:42:F821" in found[0].evidence_refs[0]


def test_an_inconclusive_static_run_is_not_a_finding():
    data = _rich_input(static_checks={"ran": True, "inconclusive": True,
                                      "findings": []})
    assert discovery.discover(data, sources=["static_check"]) == ()


def test_tests_that_failed_and_a_change_with_no_test_are_both_proposed():
    found = discovery.discover(_rich_input(), sources=["tests"])
    categories = {c.category for c in found}
    assert categories == {"verification", "coverage"}
    coverage = next(c for c in found if c.category == "coverage")
    # It names the file that has to come to exist, in the runner's own
    # convention, so `resolved_by` can see it arrive.
    assert coverage.resources == ("tests/test_auth.py",)


def test_an_absent_test_report_is_not_a_missing_test_run():
    data = _rich_input(tests={})
    assert discovery.discover(data, sources=["tests"]) == ()

    data = _rich_input(tests={"ran": False, "label": "pytest",
                              "summary": "no runner detected"})
    found = discovery.discover(data, sources=["tests"])
    assert len(found) == 1 and found[0].dedupe_key == "tests:not_run"


def test_the_changeset_reports_both_directions_of_the_gap():
    found = discovery.discover(_rich_input(), sources=["changeset"])
    by_category = {c.category: c for c in found}
    assert "verification" in by_category      # claimed and cannot be seen
    assert "consistency" in by_category       # changed and was not said
    assert by_category["verification"].resources == ("src/session.py",)
    assert by_category["consistency"].resources == ("src/auth.py",)


def test_a_proof_short_of_proved_carries_the_heaviest_uncertainty():
    found = discovery.discover(_rich_input(), sources=["proof"])
    assert len(found) == 1
    assert "no_verification_runner" in found[0].evidence_refs[0]

    proved = _rich_input(proof={"verdict": "proved", "confidence": 1.0,
                                "uncertainty": []})
    assert discovery.discover(proved, sources=["proof"]) == ()


def test_a_regression_is_core_and_an_incidental_change_is_bonus():
    found = discovery.discover(_rich_input(), sources=["delta"])
    layers = {c.title: c.layer for c in found}
    assert layers["repair the regression at src/auth.py"] == "core"
    assert layers["account for the unasked change at src/auth.py"] == "bonus"
    assert any(c.layer == "core" and "nobody observed" in c.title for c in found)


def test_an_incidental_change_outside_the_envelope_is_drift_not_an_opportunity():
    data = _rich_input(delta={"assertions": [
        {"id": "a9", "path": "static/js/app.js", "classification": "incidental",
         "operation": "modified"}]})
    assert discovery.discover(data, sources=["delta"]) == ()


def test_an_out_of_scope_change_stays_reportable_instead_of_refusing_itself():
    """It names no resource on purpose: the work is to ACCOUNT for the change,
    and a candidate carrying the out-of-scope path would be refused by
    `admits` -- losing the obligation to mention it at all."""
    found = discovery.discover(_rich_input(), sources=["delta"])
    drift = next(c for c in found if "outside the intent" in c.title)
    assert drift.resources == ()
    assert scope.admits(drift, _envelope()) == (True, "")


def test_the_state_mirror_separates_this_run_from_somebody_elses_work():
    found = discovery.discover(_rich_input(), sources=["state_mirror"])
    mine = next(c for c in found if "run_1" in c.title)
    theirs = next(c for c in found if "obj_9" in c.title)
    assert mine.relation == "downstream"
    assert theirs.relation == "opportunistic"
    assert all(c.layer == "bonus" for c in found)


def test_similar_case_produces_nothing_and_that_is_the_point():
    """§7's `similar_case` needs a structural index this phase does not have.
    The alternative is a name heuristic, which §13 and §30 forbid outright."""
    assert discovery.discover(_rich_input(), sources=["similar_case"]) == ()


def test_the_playbook_falls_back_to_the_contracts_own_expectations():
    found = discovery.discover(_rich_input(), sources=["playbook"])
    assert len(found) == 1
    assert found[0].layer == "professional"
    assert "docs/api.md" in found[0].title


def test_a_playbook_module_without_the_hook_is_loud():
    module = types.ModuleType(f"{discovery.PLAYBOOK_MODULE}.silent")
    sys.modules[module.__name__] = module
    try:
        data = _rich_input(contract=_contract(playbook="silent"))
        with pytest.raises(CompletionError) as raised:
            discovery.discover(data, sources=["playbook"])
        assert discovery.PLAYBOOK_HOOK in str(raised.value)
    finally:
        del sys.modules[module.__name__]


# -- discover ---------------------------------------------------------------

def test_an_unknown_source_is_an_error_and_not_an_empty_frontier():
    with pytest.raises(CompletionError) as raised:
        discovery.discover(_rich_input(), sources=["static_checks"])
    assert "static_check" in str(raised.value)


def test_the_answer_does_not_depend_on_the_order_the_caller_asks_in():
    one = discovery.discover(_rich_input(), sources=["tests", "static_check"])
    two = discovery.discover(_rich_input(), sources=["static_check", "tests"])
    assert [c.dedupe_key for c in one] == [c.dedupe_key for c in two]


def test_a_source_with_no_generator_yet_is_not_an_error():
    assert discovery.discover(_rich_input(), sources=["model", "reviewer"]) == ()


# -- dedupe -----------------------------------------------------------------

def _same_work(source: str, evidence: str) -> ImprovementCandidate:
    return ImprovementCandidate.parse({
        "title": f"the missing test, as {source} puts it",
        "layer": "professional", "category": "coverage", "source": source,
        "relation": "downstream", "resources": ["tests/test_auth.py"],
        "dedupe_key": "tests:no_related_test:src/auth.py",
        "evidence_refs": [evidence],
    }, "candidate")


def test_dedupe_keeps_the_more_deterministic_source_and_unions_the_evidence():
    from_model = _same_work("model", "model:it looks untested")
    from_tests = _same_work("tests", "tests:no_related_test:src/auth.py")

    for order in ((from_model, from_tests), (from_tests, from_model)):
        merged = discovery.dedupe(order)
        assert len(merged) == 1
        assert merged[0].source == "tests", "arrival order decided the winner"
        assert set(merged[0].evidence_refs) == {"model:it looks untested",
                                                "tests:no_related_test:src/auth.py"}


def test_dedupe_does_not_widen_the_survivors_footprint():
    """Two candidates may share a `dedupe_key` and name different files.
    Unioning those would silently give the survivor reach neither generator saw."""
    wide = ImprovementCandidate.parse({
        "title": "the same work, wider", "layer": "bonus", "source": "model",
        "dedupe_key": "shared", "resources": ["src/auth.py", "static/js/app.js"],
        "evidence_refs": ["model:hunch"]}, "candidate")
    narrow = ImprovementCandidate.parse({
        "title": "the same work", "layer": "bonus", "source": "static_check",
        "dedupe_key": "shared", "resources": ["src/auth.py"],
        "evidence_refs": ["static_check:ruff:src/auth.py:1:F401"]}, "candidate")

    merged = discovery.dedupe([wide, narrow])
    assert len(merged) == 1
    assert merged[0].resources == ("src/auth.py",)


def test_dedupe_leaves_different_work_alone():
    data = _rich_input()
    found = discovery.discover(data)
    assert len({c.key for c in found}) == len(found)


# -- resolved_by (§9) -------------------------------------------------------

def test_a_missing_test_that_now_exists_is_retired():
    found = discovery.discover(_rich_input(), sources=["tests"])
    coverage = next(c for c in found if c.category == "coverage")

    still_open = discovery.resolved_by(found, ledger_summary={"mutations": []})
    assert coverage.id not in still_open

    settled = discovery.resolved_by(
        found, ledger_summary={"mutations": ["src/auth.py", "tests/test_auth.py"]})
    assert coverage.id in settled


def test_a_delta_finding_that_disappeared_is_retired():
    found = discovery.discover(_rich_input(), sources=["delta"])
    assert found

    unchanged = discovery.resolved_by(
        found, ledger_summary={"mutations": []}, delta=_rich_input().delta)
    assert unchanged == ()

    gone = discovery.resolved_by(found, ledger_summary={"mutations": []},
                                 delta={"assertions": []})
    assert set(gone) == {c.id for c in found}


def test_a_regression_at_the_same_path_keeps_the_candidate_open():
    found = discovery.discover(_rich_input(), sources=["tests"])
    coverage = next(c for c in found if c.category == "coverage")
    ledger = {"mutations": ["src/auth.py", "tests/test_auth.py"]}
    delta = {"assertions": [{"id": "a1", "path": "tests/test_auth.py",
                             "classification": "regression",
                             "operation": "modified"}]}
    assert coverage.id not in discovery.resolved_by(found, ledger_summary=ledger,
                                                    delta=delta)


def test_a_static_finding_is_never_retired_by_a_write_to_its_file():
    """A timestamp is not a fix. Retiring on a mutation would drop a real
    defect the moment anything touched the file for any other reason."""
    found = discovery.discover(_rich_input(), sources=["static_check"])
    assert discovery.resolved_by(
        found, ledger_summary={"mutations": ["src/auth.py"]}) == ()


# -- determinism ------------------------------------------------------------

#: Names that would mean a model produced a candidate. `stream_agent_loop` and
#: `chat` are this repository's own entry points; the rest are the shapes every
#: client library uses.
_MODEL_CALLS = frozenset({
    "chat", "chat_completion", "complete", "completion", "completions",
    "generate", "generate_text", "stream", "stream_agent_loop", "call_model",
    "ask_model", "predict", "infer", "embed", "llm", "prompt", "run_model",
    "acompletion", "invoke", "sample", "respond",
})


def test_no_generator_calls_a_model():
    tree = ast.parse(inspect.getsource(discovery))
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            called.add(func.id)
        elif isinstance(func, ast.Attribute):
            called.add(func.attr)
    offenders = sorted(called & _MODEL_CALLS)
    assert not offenders, f"discovery.py calls {offenders}"


def test_discovery_imports_nothing_that_can_reach_a_model():
    tree = ast.parse(inspect.getsource(discovery))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for forbidden in ("openai", "httpx", "requests", "aiohttp", "src.llm",
                      "src.agent_loop", "src.auto_review"):
        assert forbidden not in imported


def test_the_same_input_answers_the_same_thing_twice():
    data = _rich_input()
    first = [(c.source, c.dedupe_key, c.layer) for c in discovery.discover(data)]
    second = [(c.source, c.dedupe_key, c.layer) for c in discovery.discover(data)]
    assert first == second
