"""The scope envelope: what it may narrow, and what it must never widen.

§27's "Scope" block, pinned. Six claims are load-bearing enough that prose
would not have kept them true:

1. An instruction only ever narrows, and it narrows a `greedy` run too. The
   envelope it was called on is left exactly as it was.
2. A protected resource inside the allow list is `unrelated` and is refused.
   That is the case a whitelist-only boundary gets wrong.
3. Permission is checked BEFORE value. The test uses a candidate with
   `expected_value = 1.0` and no permission, and then reads this module's own
   syntax tree to show that no score is consulted at all -- behaviour can be
   right today and one new line can make it wrong tomorrow.
4. A forbidden effect is refused even when the file is inside the envelope.
5. A global refactor and a new dependency both fail at the boundary.
6. A relation is read off paths. A goal that names a function cannot promote a
   file to `direct` by sharing a word with it.

Two `xfail(strict=True)` tests at the end record DEFECTS IN FROZEN CODE
(`ScopeEnvelope.narrow`). They are not this module's bugs and they are not
worked around silently: `scope.py` does its own containment-aware intersection
and says why in `_intersect_resources`.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from src.agent_profiles.contracts import PermissionEnvelope
from src.completion_engine import scope
from src.completion_engine.contracts import (
    GREEDY_RELATIONS,
    ImprovementCandidate,
    ScopeEnvelope,
)


def _candidate(**overrides) -> ImprovementCandidate:
    payload = {"title": "a candidate", "layer": "bonus", "relation": "direct",
               "resources": ["src/auth.py"]}
    payload.update(overrides)
    return ImprovementCandidate.parse(payload, "candidate")


def _greedy_envelope(**overrides) -> ScopeEnvelope:
    kwargs = {"goal": "fix the expired-token bug in src/auth.py",
              "mode": "greedy", "touched": ["src/auth.py"]}
    kwargs.update(overrides)
    return scope.compile_envelope(**kwargs)


# -- compiling ---------------------------------------------------------------

def test_a_greedy_envelope_reaches_the_component_and_its_tests():
    envelope = _greedy_envelope()
    assert envelope.allowed_relations == GREEDY_RELATIONS
    assert envelope.covers("src/auth.py")
    assert envelope.covers("src/session.py")      # same component
    assert envelope.covers("tests/test_auth.py")  # where the regression test goes
    assert not envelope.covers("static/js/app.js")


def test_a_literal_envelope_reaches_only_what_was_named():
    envelope = _greedy_envelope(mode="literal")
    assert envelope.allowed_relations == ("direct",)
    assert envelope.covers("src/auth.py")
    assert not envelope.covers("src/session.py")
    assert not envelope.covers("tests/test_auth.py")


def test_a_request_that_names_nothing_is_bounded_by_permission_not_by_paths():
    envelope = scope.compile_envelope(goal="make the app faster", mode="greedy")
    assert envelope.allowed_resources == ()
    assert envelope.covers("anything/at/all.py")
    assert any("named no resource" in note for note in envelope.ambiguities)


def test_an_envelope_restates_what_the_run_cannot_do_and_never_what_it_can():
    """The deny half of `PermissionEnvelope` is safe to mirror; the allow half
    is not, because the two read empty in opposite directions."""
    permissions = PermissionEnvelope(tools=("read_file",), effects=("read_workspace",))
    envelope = _greedy_envelope(permissions=permissions)
    assert envelope.allowed_effects == ()
    assert "read_workspace" not in envelope.forbidden_effects
    assert "destructive" in envelope.forbidden_effects
    assert envelope.permits_effect("read_workspace")
    assert not envelope.permits_effect("destructive")


# -- narrowing (§27: "«solo X» protege resto") -------------------------------

def test_only_change_this_line_narrows_a_greedy_run_and_leaves_the_original_alone():
    envelope = _greedy_envelope()
    narrowed = scope.narrow_from_instruction(envelope, "solo cambia esta línea")

    assert narrowed.allowed_relations == ("direct",)
    assert envelope.allowed_relations == GREEDY_RELATIONS, "the original was mutated"
    # What it could not read is recorded literally rather than guessed at.
    assert any("unread scope phrase" in note for note in narrowed.ambiguities)


def test_only_touch_one_file_shrinks_the_working_area_to_that_file():
    envelope = _greedy_envelope()
    assert envelope.covers("src/session.py")

    narrowed = scope.narrow_from_instruction(envelope, "only touch src/auth.py")
    assert narrowed.covers("src/auth.py")
    assert not narrowed.covers("src/session.py")


def test_a_negated_only_does_not_narrow():
    envelope = _greedy_envelope()
    narrowed = scope.narrow_from_instruction(envelope, "no solo src/auth.py")
    assert narrowed.covers("src/session.py")


def test_do_not_touch_adds_a_protected_resource_and_never_removes_one():
    envelope = _greedy_envelope(protected=["src/secrets.py"])
    narrowed = scope.narrow_from_instruction(
        envelope, "arregla el login, sin tocar src/settings.py")

    assert "src/settings.py" in narrowed.protected_resources
    assert "src/secrets.py" in narrowed.protected_resources


def test_an_instruction_naming_a_file_outside_the_envelope_does_not_widen_it():
    envelope = _greedy_envelope()
    narrowed = scope.narrow_from_instruction(envelope, "only touch docs/README.md")

    assert not narrowed.covers("docs/README.md")
    assert narrowed.allowed_resources == envelope.allowed_resources
    assert any("outside this envelope" in note for note in narrowed.ambiguities)


def test_an_instruction_can_never_reach_a_wider_mode_than_the_envelope_holds():
    envelope = _greedy_envelope(mode="literal")
    narrowed = scope.narrow_from_instruction(envelope, "dame muchas opciones")
    assert narrowed.allowed_relations == ("direct",)


# -- relation (§6) -----------------------------------------------------------

def test_a_protected_resource_inside_the_allow_list_is_unrelated():
    envelope = _greedy_envelope(protected=["src/settings.py"])
    assert "src" in envelope.allowed_resources          # the allow list holds it
    assert envelope.protects("src/settings.py")

    assert scope.relation_of("src/settings.py", envelope,
                             touched=["src/auth.py"]) == "unrelated"


def test_a_protected_resource_is_refused_out_of_scope():
    envelope = _greedy_envelope(protected=["src/settings.py"])
    candidate = _candidate(resources=["src/settings.py"], relation="adjacent")
    assert scope.admits(candidate, envelope) == (False, "out_of_scope")


def test_the_relations_are_read_off_the_tree():
    envelope = _greedy_envelope()
    touched = ["src/auth.py"]
    assert scope.relation_of("src/auth.py", envelope, touched=touched) == "direct"
    assert scope.relation_of("src/session.py", envelope, touched=touched) == "adjacent"
    assert scope.relation_of("tests/test_auth.py", envelope, touched=touched) == "downstream"
    assert scope.relation_of("static/js/app.js", envelope, touched=touched) == "unrelated"


def test_the_same_file_somewhere_else_is_a_similar_case():
    envelope = scope.compile_envelope(goal="fix apps/a/views.py", mode="greedy",
                                      touched=["apps/a/views.py", "apps"])
    assert scope.relation_of("apps/b/views.py", envelope,
                             touched=["apps/a/views.py"]) == "similar_case"


def test_something_inside_the_envelope_with_no_relation_is_opportunistic():
    envelope = scope.compile_envelope(goal="work on src/auth.py", mode="greedy",
                                      touched=["src/auth.py", "src"])
    assert scope.relation_of("src/billing/invoice.py", envelope,
                             touched=["src/auth.py"]) == "opportunistic"


def test_relation_of_never_reads_a_function_name():
    """§13 and §30: a name is not a relation. A goal full of identifiers must
    not be able to pull an unrelated file closer than the tree says it is."""
    envelope = _greedy_envelope()
    identifiers = ("validate_user", "session", "auth", "billing", "invoice")

    plain = scope.relation_of("src/billing/invoice.py", envelope,
                              touched=["src/auth.py"])
    loaded = scope.relation_of("src/billing/invoice.py", envelope,
                               touched=["src/auth.py"], goal_terms=identifiers)
    assert plain == loaded == "opportunistic"

    # And the goal term has to look like a resource before it counts at all.
    assert scope.relation_of("src/billing/invoice.py", envelope,
                             touched=[], goal_terms=("invoice",)) != "direct"
    assert scope.relation_of("src/billing/invoice.py", envelope, touched=[],
                             goal_terms=("src/billing/invoice.py",)) == "direct"


# -- admits: the order of the gates (rule 1) ---------------------------------

def test_a_candidate_without_the_permission_is_refused_before_it_is_scored():
    """The candidate is worth 1.0 and costs nothing. It still loses, and it
    loses on the permission gate rather than on any comparison of value."""
    envelope = _greedy_envelope()
    candidate = _candidate(expected_value=1.0, estimated_cost=0.0, risk=0.0,
                           confidence=1.0, required_permissions=["write_file"])
    permissions = PermissionEnvelope(tools=("read_file",), effects=("read_workspace",))

    assert scope.admits(candidate, envelope, permissions=permissions) == \
        (False, "no_permission")


def test_admits_never_reads_a_score(   ):
    """Mechanised rather than observed: the syntax tree of `admits` is walked
    and must not mention a scoring field. A behavioural check would pass on the
    day somebody added `if candidate.expected_value > 0.9: return True, ""`."""
    tree = ast.parse(inspect.getsource(scope.admits))
    touched = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for scoring_field in ("expected_value", "estimated_cost", "risk", "confidence"):
        assert scoring_field not in touched


def test_a_candidate_that_needs_something_is_refused_when_no_permissions_are_given():
    envelope = _greedy_envelope()
    needs = _candidate(required_permissions=["write_file"])
    needs_nothing = _candidate()

    assert scope.admits(needs, envelope) == (False, "no_permission")
    assert scope.admits(needs_nothing, envelope) == (True, "")


def test_a_forbidden_effect_is_refused_even_inside_the_envelope():
    envelope = _greedy_envelope().narrow(effects=("write_workspace",))
    permissions = PermissionEnvelope(tools=("write_file",),
                                     effects=("write_workspace", "network_egress"))
    candidate = _candidate(resources=["src/auth.py"],
                           required_effects=["network_egress"])

    assert envelope.covers("src/auth.py")
    assert scope.admits(candidate, envelope, permissions=permissions) == \
        (False, "forbidden_effect")


def test_a_global_refactor_is_refused_on_scope():
    envelope = _greedy_envelope()
    refactor = _candidate(title="rename the config helper across the repository",
                          resources=["src/auth.py", "static/js/app.js"],
                          relation="similar_case")
    assert scope.admits(refactor, envelope) == (False, "out_of_scope")


def test_a_new_dependency_cannot_get_in_without_crossing_the_boundary():
    """Both halves of §27's "dependency nueva requiere boundary": the file it
    would edit is outside the working area, and the effect it would need is
    outside the envelope."""
    permissions = PermissionEnvelope(tools=("write_file",),
                                     effects=("write_workspace", "network_egress"))
    envelope = _greedy_envelope(permissions=permissions).narrow(
        effects=("write_workspace",))

    by_file = _candidate(title="add `requests` to requirements.txt",
                         resources=["requirements.txt"])
    assert scope.admits(by_file, envelope, permissions=permissions) == \
        (False, "out_of_scope")

    by_effect = _candidate(title="install `requests`", resources=["src/auth.py"],
                           required_effects=["network_egress"])
    assert scope.admits(by_effect, envelope, permissions=permissions) == \
        (False, "forbidden_effect")


def test_a_relation_the_mode_does_not_reach_is_refused_on_scope():
    envelope = _greedy_envelope(mode="professional")
    candidate = _candidate(relation="similar_case", resources=["src/auth.py"])
    assert "similar_case" not in envelope.allowed_relations
    assert scope.admits(candidate, envelope) == (False, "out_of_scope")


def test_describe_says_out_loud_when_an_envelope_is_unbounded():
    unbounded = scope.compile_envelope(goal="make the app faster", mode="greedy")
    assert "anywhere" in scope.describe(unbounded)
    bounded = _greedy_envelope(protected=["src/settings.py"])
    described = scope.describe(bounded)
    assert "src/auth.py" in described and "src/settings.py" in described


# -- two defects found here and since FIXED, kept as regression guards -------
#
# Both were real, both were in `ScopeEnvelope.narrow`, and both are the kind
# that come back: the first because a set intersection looks obviously correct
# until you notice what an empty result MEANS, and the second because a
# uniqueness rule on a list is invisible from the call site.

def test_narrowing_to_one_file_does_not_open_the_whole_tree():
    envelope = scope.compile_envelope(goal="work in src/", mode="greedy")
    assert envelope.allowed_resources and not envelope.covers("static/js/app.js")

    narrowed = envelope.narrow(resources=["src/auth.py"])
    assert narrowed.covers("src/auth.py")
    assert not narrowed.covers("static/js/app.js"), \
        "narrowing to one file opened the whole tree"


def test_the_same_narrowing_twice_is_not_an_error():
    """The same instruction re-read on the next round must not raise.

    `narrow` appends `narrowed: <reason>` to `ambiguities`, which `text_list`
    parses with `unique=True`; the second identical narrowing used to raise a
    duplicate error -- which is to say it failed exactly when the user was
    being most consistent.
    """
    envelope = _greedy_envelope()
    once = envelope.narrow(relations=("direct",), reason="only this line")
    twice = once.narrow(relations=("direct",), reason="only this line")
    assert twice.allowed_relations == ("direct",)
