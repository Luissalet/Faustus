"""The coding playbook: the real chain, the §13 limit, and no second process.

The claims this file exists to keep true:

1. The chain the playbook describes EXISTS. Every module and function name in
   `code.CHAIN` is imported and looked up, so the day somebody renames
   `static_checks.run_for_turn` this file fails instead of the description
   quietly becoming fiction -- and a fictional description of the professional
   layer is what makes the next author build a parallel one.
2. A candidate that asks for a global refactor, a framework change or a mass
   dependency update comes out with `relation="unrelated"` and the SCOPE
   ENVELOPE refuses it. The playbook does not filter it: a refusal that was
   recorded is auditable and a candidate that was never emitted is not.
3. Adding a playbook never makes the engine check LESS. `_from_playbook` stops
   running its `professional_expectations` fallback the moment a playbook
   module exists, so the playbook has to cover those lines itself.
4. Nothing here calls a model, and the same input answers the same thing twice.
5. An absent report produces nothing. "The analyser did not report" and "the
   analyser reported that it could not run" are different facts.
"""

from __future__ import annotations

import ast
import importlib
import inspect

import pytest

from src.agent_profiles.completion import COMPLETION_MODES, policy_for
from src.completion_engine import discovery, playbooks, scope
from src.completion_engine.contracts import (
    LAYERS,
    CompletionContract,
    CompletionError,
    ImprovementCandidate,
)
from src.completion_engine.playbooks import code


def _contract(**overrides) -> CompletionContract:
    payload = {
        "mode": "greedy",
        "playbook": "code",
        "core_deliverables": ["src/auth.py rejects an expired token"],
        "professional_expectations": ["docs/api.md matches the new signature"],
        "verification": ["run pytest tests/test_auth.py"],
        "run_id": "run_1",
    }
    payload.update(overrides)
    return CompletionContract.parse(payload, "completion_contract")


def _envelope():
    return scope.compile_envelope(goal="fix the expired-token bug in src/auth.py",
                                  mode="greedy", touched=["src/auth.py"])


def _input(**overrides) -> discovery.DiscoveryInput:
    kwargs = dict(
        contract=_contract(),
        envelope=_envelope(),
        ledger_summary={"mutations": ["src/auth.py"], "static_analysis": None,
                        "tests": None, "review": None},
        static_checks={"ran": False, "inconclusive": True, "tools": [],
                       "unavailable": "install ruff (pip install ruff)",
                       "summary": "install ruff (pip install ruff)"},
        tests={"ran": True, "inconclusive": True, "label": "pytest",
               "command": "pytest -q", "summary": "the runner could not be read"},
        workspace="",
    )
    kwargs.update(overrides)
    return discovery.DiscoveryInput(**kwargs)


# -- 1. the chain the playbook names is the chain that exists ---------------

def test_every_name_the_chain_cites_really_exists():
    """The deliverable of `CHAIN`. A docstring goes stale in silence; this does
    not, because it imports the module and looks the attribute up."""
    assert code.CHAIN, "the coding playbook must describe the turn's own chain"
    for step in code.CHAIN:
        module = importlib.import_module(step.module)
        for name in step.names():
            assert hasattr(module, name), \
                f"{step.module}.{name} is cited by CHAIN step {step.step!r} " \
                f"and does not exist"


def test_the_chain_names_the_four_modules_the_turn_actually_runs():
    """Named individually so a step SILENTLY DISAPPEARING fails too -- the loop
    above would still pass over a `CHAIN` that had been emptied of one."""
    modules = {step.module for step in code.CHAIN}
    for expected in ("src.agent_harness", "src.static_checks",
                     "src.project_tests", "src.auto_review", "src.changesets"):
        assert expected in modules, f"the chain does not mention {expected}"


def test_every_ledger_slot_the_chain_names_is_a_real_turn_ledger_field():
    """The slots are how the playbook reads whether a step ran. A slot that is
    not a `TurnLedger.summary()` key would read `None` forever and the playbook
    would report a gap on every turn."""
    from src.agent_harness import TurnLedger

    summary = TurnLedger(workspace="", user_text="").summary()
    for step in code.CHAIN:
        if not step.ledger_slot:
            continue
        assert step.ledger_slot in summary, \
            f"{step.ledger_slot!r} is not a key of TurnLedger.summary()"


def test_every_chain_step_says_which_professional_line_it_discharges():
    """`discharges` is the mapping from a step of the turn's chain to the §13
    line it is evidence for. An empty one would make `CHAIN` a list of function
    names with no argument for why any of them is the professional layer."""
    for step in code.CHAIN:
        assert step.step and step.module and step.entry
        assert step.discharges, f"{step.step} does not say what it discharges"


def test_the_two_chain_steps_with_no_generator_are_the_documented_ones():
    """Syntax and review are described and never read, and the module says why.
    A third silent step would be an undocumented hole in the checklist."""
    reading = {"static_analysis", "tests"}
    documented_silent = {"syntax", "review", "proof"}
    assert {s.step for s in code.CHAIN} == reading | documented_silent
    doc = code.__doc__ or ""
    assert "**syntax**" in doc and "**review**" in doc


def test_the_playbook_hook_is_the_one_discovery_looks_for():
    assert playbooks.HOOK == discovery.PLAYBOOK_HOOK
    assert discovery.PLAYBOOK_MODULE == playbooks.__name__
    assert hasattr(code, discovery.PLAYBOOK_HOOK)
    assert hasattr(playbooks, discovery.PLAYBOOK_HOOK)


# -- 2. §13's limit is a check, not a comment -------------------------------

@pytest.mark.parametrize("text,expected", [
    ("refactor the whole codebase before shipping", "global_refactor"),
    ("a global refactor of the auth package", "global_refactor"),
    ("rewrite everything for consistency", "global_refactor"),
    ("migrate to another framework", "framework_change"),
    ("swap the framework for something newer", "framework_change"),
    ("upgrade all dependencies", "mass_dependency_update"),
    ("bump every dependency to latest", "mass_dependency_update"),
    ("refactor the login handler", ""),
    ("add a regression test for src/auth.py", ""),
    ("fix the serializer inside the framework", ""),
    ("update the dependency on requests", ""),
    ("", ""),
])
def test_the_limit_matcher_says_which_boundary_a_line_would_cross(text, expected):
    assert code.crosses_the_limit(text) == expected


def test_every_limit_has_a_phrase_a_receipt_can_print():
    for name, phrase in code.LIMITS:
        assert code.limit_phrase(name) == phrase and phrase
    assert code.limit_phrase("not_a_limit") == ""


def test_a_global_refactor_candidate_is_unrelated_and_the_envelope_refuses_it():
    """§13's Límite. The playbook does NOT drop the candidate: it labels the
    relation honestly and `scope.admits` does the refusing, so the decision is
    recorded with a reason instead of vanishing."""
    data = _input(contract=_contract(professional_expectations=[
        "refactor the whole codebase for consistency"]))
    produced = code.candidates(data)
    refactor = [c for c in produced if "refactor" in c.title]
    assert len(refactor) == 1, "the boundary-crossing line must still be emitted"
    candidate = refactor[0]
    assert candidate.relation == "unrelated"

    ok, reason = scope.admits(candidate, data.envelope)
    assert (ok, reason) == (False, "out_of_scope")


def test_the_relation_is_what_the_envelope_rejects_on():
    """Proves the previous test is about the RELATION and not about a resource
    that happened to fall outside the working area: the same candidate with a
    `direct` relation is admitted by the same envelope."""
    data = _input(contract=_contract(professional_expectations=[
        "refactor the whole codebase for consistency"]))
    candidate = [c for c in code.candidates(data) if "refactor" in c.title][0]
    assert not candidate.resources, "this line names no file; only the relation is left"

    payload = candidate.to_dict()
    payload["relation"] = "direct"
    twin = ImprovementCandidate.parse(payload, "candidate")
    assert scope.admits(twin, data.envelope) == (True, "")
    assert scope.admits(candidate, data.envelope) == (False, "out_of_scope")


def test_an_ordinary_expectation_is_direct_and_admitted():
    """`src/session.py` is inside the envelope (the goal named `src/auth.py`,
    so the component is `src/`) and the turn did not touch it -- an uncovered
    expectation on a file the run may work in."""
    data = _input(contract=_contract(
        professional_expectations=["src/session.py raises a typed error"]))
    candidate = [c for c in code.candidates(data)
                 if "typed error" in c.title][0]
    assert candidate.relation == "direct"
    assert candidate.resources == ("src/session.py",)
    assert scope.admits(candidate, data.envelope) == (True, "")


# -- 3. a playbook never checks less than the fallback it replaces ----------

def test_the_playbook_still_checks_the_contracts_professional_expectations():
    """`_from_playbook` returns the playbook's output and never reaches its own
    fallback loop once a module exists. Without this the engine would check
    strictly LESS for the one domain that has a checklist."""
    data = _input(contract=_contract(
        professional_expectations=["docs/api.md matches the new signature"]))
    keys = {c.dedupe_key for c in code.candidates(data)}
    assert "playbook:professional_expectation:docs/api.md matches the new signature" \
        in keys


def test_the_expectation_key_matches_the_fallbacks_so_the_two_would_dedupe():
    """The playbook's key for an expectation is `_from_playbook`'s own spelling.
    If both ever ran, `dedupe` would see one candidate rather than two
    descriptions of one gap."""
    line = "docs/api.md matches the new signature"
    data = _input(contract=_contract(professional_expectations=[line]))
    from_playbook = [c for c in code.candidates(data) if line in c.title][0]

    bare = _contract(playbook="", professional_expectations=[line])
    fallback_data = discovery.DiscoveryInput(contract=bare, envelope=_envelope(),
                                             ledger_summary={"mutations": []})
    from_fallback = [c for c in discovery.discover(fallback_data)
                     if line in c.title][0]
    assert from_playbook.key == from_fallback.key


def test_an_expectation_the_turn_covered_is_not_reported():
    data = _input(contract=_contract(
        professional_expectations=["src/auth.py raises a typed error"]),
        ledger_summary={"mutations": ["src/auth.py"]})
    assert not [c for c in code.candidates(data) if "typed error" in c.title]


def test_the_playbook_goes_through_discovery_unchanged():
    """The seam end to end: `discover` finds the module, calls the hook, and
    the rows survive `dedupe`."""
    data = _input()
    through = {c.dedupe_key for c in discovery.discover(data)}
    direct = {c.dedupe_key for c in code.candidates(data)}
    assert direct and direct <= through


def test_the_package_hook_routes_to_the_same_answer():
    data = _input()
    assert [c.dedupe_key for c in playbooks.candidates(data)] == \
           [c.dedupe_key for c in code.candidates(data)]


def test_the_package_hook_falls_back_to_the_envelopes_domain():
    """A contract with no `playbook` field still reaches the coding checklist
    when the envelope says the run is about code."""
    data = _input(contract=_contract(playbook=""))
    assert "code" in data.envelope.allowed_domains
    assert playbooks.candidates(data)


def test_an_unknown_playbook_name_answers_nothing_rather_than_raising():
    """Discovery's own fallback covers the typo; raising here would turn a
    misspelled contract field into a failed turn."""
    data = _input(contract=_contract(playbook="cod"))
    assert playbooks.candidates(data) == ()
    assert playbooks.playbook_for("cod") is None
    assert playbooks.playbook_for("code") is code
    assert playbooks.known() == ("code",)


# -- 4. determinism, evidence, and no model --------------------------------

_MODEL_CALLS = frozenset({
    "chat", "chat_completion", "complete", "completion", "completions",
    "generate", "generate_text", "stream", "stream_agent_loop", "call_model",
    "ask_model", "predict", "infer", "embed", "llm", "prompt", "run_model",
    "acompletion", "invoke", "sample", "respond",
})


@pytest.mark.parametrize("module", [code, playbooks])
def test_no_playbook_calls_a_model(module):
    tree = ast.parse(inspect.getsource(module))
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
    assert not offenders, f"{module.__name__} calls {offenders}"


@pytest.mark.parametrize("module", [code, playbooks])
def test_no_playbook_imports_anything_that_can_reach_a_model(module):
    """`auto_review` is NAMED in `CHAIN` as a string and must never be
    imported: a playbook that ran the reviewer would be an assisted source
    pretending to be a deterministic checklist."""
    tree = ast.parse(inspect.getsource(module))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for forbidden in ("openai", "httpx", "requests", "aiohttp", "src.llm_core",
                      "src.agent_loop", "src.auto_review", "src.ai_interaction"):
        assert forbidden not in imported


def test_the_same_input_answers_the_same_thing_twice():
    data = _input()
    first = [(c.source, c.layer, c.relation, c.dedupe_key)
             for c in code.candidates(data)]
    second = [(c.source, c.layer, c.relation, c.dedupe_key)
              for c in code.candidates(data)]
    assert first == second and first


def test_every_candidate_carries_evidence_and_says_it_came_from_a_playbook():
    produced = code.candidates(_input())
    assert produced
    for candidate in produced:
        assert candidate.source == "playbook"
        assert candidate.evidence_refs, f"{candidate.title} rests on nothing"
        assert candidate.layer in ("professional", "bonus")


def test_nothing_is_priced_by_the_playbook():
    """§30: a generator that proposed work AND scored it would be the only
    voice on its value. The frontier fills these in from evidence this module
    does not hold."""
    for candidate in code.candidates(_input()):
        assert candidate.expected_value == 0.0
        assert candidate.estimated_cost == 0.0
        assert candidate.risk == 0.0
        assert candidate.confidence == 0.0


# -- 5. an absent report is not evidence ------------------------------------

def test_an_empty_input_produces_nothing_at_all():
    """No reports, no ledger, no expectations: nothing can be said, so nothing
    is. The opposite default would charge every run with every gap."""
    bare = discovery.DiscoveryInput(
        contract=_contract(professional_expectations=[], verification=[]),
        envelope=_envelope())
    assert code.candidates(bare) == ()


def test_an_absent_static_report_is_not_a_gap():
    data = _input(static_checks={})
    assert not [c for c in code.candidates(data) if "static-analysis" in c.title]


def test_a_clean_static_report_is_not_a_gap():
    """`ran` with nothing to say is the healthy case and must not produce a
    candidate on every turn."""
    data = _input(static_checks={"ran": True, "inconclusive": False, "ok": True,
                                 "summary": "clean (ruff)", "tools": ["ruff"]})
    assert not [c for c in code.candidates(data) if "static-analysis" in c.title]


def test_a_static_report_with_nothing_to_check_is_not_a_gap():
    """`run_for_turn` only fills `unavailable` when a file with a known checker
    extension changed. A documentation-only turn leaves it empty, and reporting
    that as a hole would put a candidate on every one of them."""
    data = _input(static_checks={"ran": False, "inconclusive": True,
                                 "unavailable": "",
                                 "summary": "no analysable file changed"})
    assert not [c for c in code.candidates(data) if "static-analysis" in c.title]


def test_an_uninstalled_analyser_is_a_gap_and_carries_the_install_hint():
    candidate = [c for c in code.candidates(_input())
                 if "static-analysis" in c.title][0]
    assert candidate.category == "verification"
    assert "install ruff" in candidate.detail
    assert candidate.verification


def test_an_inconclusive_test_run_is_a_gap_and_a_green_one_is_not():
    gap = [c for c in code.candidates(_input()) if "inconclusive" in c.title]
    assert len(gap) == 1 and gap[0].category == "verification"

    green = _input(tests={"ran": True, "ok": True, "inconclusive": False,
                          "label": "pytest", "summary": "12 passed"})
    assert not [c for c in code.candidates(green) if "inconclusive" in c.title]


def test_an_absent_test_report_is_not_a_gap():
    """`project_tests.run_for_turn` answers None when the feature is off or the
    project has no runner. Charging a turn with a gap it was configured not to
    have is the mistake `DiscoveryInput`'s docstring names."""
    data = _input(tests={})
    assert not [c for c in code.candidates(data) if "inconclusive" in c.title]


def test_a_promised_verification_is_reported_only_when_nothing_ran():
    promised = [c for c in code.candidates(_input())
                if c.dedupe_key.startswith("playbook:verification_promised:")]
    assert len(promised) == 1

    ran = _input(ledger_summary={"mutations": ["src/auth.py"],
                                 "tests": {"ran": True, "ok": True},
                                 "static_analysis": None, "review": None})
    assert not [c for c in code.candidates(ran)
                if c.dedupe_key.startswith("playbook:verification_promised:")]


def test_no_ledger_means_no_verification_claim():
    """An empty `ledger_summary` is no record to read, not a record saying
    nothing happened."""
    data = _input(ledger_summary={})
    assert not [c for c in code.candidates(data)
                if c.dedupe_key.startswith("playbook:verification_promised:")]


def test_the_bonus_layer_produces_nothing_and_says_why():
    """§13's greedy column has no deterministic evidence in this build. The
    empty generator is the deliverable; a plausible one nobody could check
    would be §30's "no afirmar required sin relación demostrable"."""
    assert not [c for c in code.candidates(_input()) if c.layer == "bonus"]
    assert code.BONUS, "the checklist is still published for a contract to use"
    assert "_from_similar_case" in inspect.getsource(code._from_bonus)


# -- the checklist as data --------------------------------------------------

def test_definition_of_done_is_cumulative_in_the_contracts_layer_order():
    core = code.definition_of_done(layer="core")
    professional = code.definition_of_done(layer="professional")
    bonus = code.definition_of_done(layer="bonus")
    assert core == code.CORE
    assert professional == code.CORE + code.PROFESSIONAL
    assert bonus == code.CORE + code.PROFESSIONAL + code.BONUS
    assert list(LAYERS[:3]) == ["core", "professional", "bonus"]


def test_an_exploratory_definition_of_done_raises_rather_than_lying():
    """§13's maximalist column is not written here, and answering with the
    bonus list would report work as checked that nobody listed."""
    assert "exploratory" in LAYERS
    with pytest.raises(CompletionError) as raised:
        code.definition_of_done(layer="exploratory")
    assert "exploratory" in str(raised.value)


def test_an_unknown_layer_raises():
    with pytest.raises(CompletionError):
        code.definition_of_done(layer="professionel")


def test_expectations_follow_the_policy_and_never_a_second_table():
    assert code.expectations(mode="literal") == ()
    assert code.expectations(mode="professional") == code.PROFESSIONAL
    assert code.expectations(mode="greedy") == code.PROFESSIONAL + code.BONUS
    for mode in COMPLETION_MODES:
        allowed = 1 + max(0, int(policy_for(mode).max_extra_layers))
        expected = tuple(
            line for name in LAYERS[:allowed] if name != "core"
            for line in {"professional": code.PROFESSIONAL,
                         "bonus": code.BONUS}.get(name, ()))
        assert code.expectations(mode=mode) == expected


def test_an_unknown_mode_raises_out_of_the_policy():
    """Falling back to `greedy` would answer a typo with the second-deepest
    mode in the product."""
    with pytest.raises(Exception):
        code.expectations(mode="agressive")


def test_the_domain_matches_the_registry_key():
    assert code.DOMAIN == "code"
    assert playbooks.PLAYBOOKS[code.DOMAIN] is code
