"""tests/test_delta_engine_core.py -- the rules the delta core must not bend.

Every test here fixes a RULE, never a snapshot. `METHOD_TIERS` will grow, the
classifier will learn new domains and the explanation will be reworded; none of
that may quietly turn an unmeasured property into a preserved one, and none of
these assertions counts rows or pins a list that is supposed to grow.

The guarantees, one test each:

* an `unknown` never becomes a `preserved`, by either word -- the guard is in
  the classifier and not only in the contract's refusal;
* coverage and confidence are two axes: covering all of something with a
  perceptual method does not produce `exact`;
* a `forbidden` path that also sits inside `allowed` is a regression, which is
  the case a scope with only `allowed` gets wrong;
* a violated security invariant outranks a perfect resemblance to a request;
* `required` is never inferred from a name that looks related;
* a rename is one `moved`, not a `missing` plus an `added`;
* identical elements on both sides produce no invented correspondence;
* a `becomes(red)` nobody satisfied is `unmet`, and the assessment is
  `mismatched`;
* a delta with no observations is `inconclusive` and never `matched`;
* an intent carrying `unknowns` never reports `matched`;
* merging coverage takes the minimum per dimension and the AND of readable;
* `propagate` does not let `exact` through an uncertain alignment.
"""

from __future__ import annotations

import hashlib

import pytest

from src.delta_engine import alignment as A
from src.delta_engine import classification as C
from src.delta_engine import confidence as CONF
from src.delta_engine import coverage as COV
from src.delta_engine import verdict as V
from src.delta_engine.adapters.base import Element, Finding, Snapshot
from src.delta_engine.contracts import (
    CONFIDENCE,
    Alignment,
    Coverage,
    DeltaError,
    IntentContract,
    InvariantResult,
    RequestedChange,
    RevisionRef,
    severity_rank,
)

OWNER = "alice"
FROZEN = "2026-09-06T12:00:00Z"


def digest(seed: str) -> str:
    """A real sha256, because `RevisionRef` refuses anything else."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def revision(name: str) -> RevisionRef:
    return RevisionRef.parse({"kind": "file", "ref": name, "hash": digest(name)})


def snapshot(name: str, elements) -> Snapshot:
    return Snapshot(revision=revision(name), elements=tuple(elements))


def element(key: str, content_hash: str = "", value: str = "") -> Element:
    return Element(key=key, hash=content_hash, value=value)


def intent(**overrides) -> IntentContract:
    payload = {"id": "intent_1", "owner": OWNER, "domain": "code", "frozen_at": FROZEN}
    payload.update(overrides)
    return IntentContract.parse(payload)


def finding(path: str, operation: str, **overrides) -> Finding:
    """A `Finding` built by its own constructor, which caps confidence by tier."""
    fields = {"path": path, "operation": operation, "confidence": "exact",
              "tier": "parser"}
    fields.update(overrides)
    return Finding(**fields)


def full_coverage(**dimensions) -> Coverage:
    """Both ends readable. The precondition every assessment rule after the
    first one assumes, so a test about rule 4 is not silently testing rule 1."""
    return COV.build(source_readable=True, target_readable=True,
                     dimensions=dimensions or None)


# -- rule 1: not detected is not preserved ---------------------------------


def test_a_property_nobody_measured_is_never_reported_as_preserved():
    """The measurement decides the word, and the same row proves it both ways."""
    contract = intent(requested=[{"path": "character.face", "condition": "preserved"}])

    unmeasured = finding("character.face", "unchanged", confidence="unknown",
                         tier="model", method="model_compare")
    classification, _, _ = C.classify(unmeasured, intent=contract)
    assert classification == "unknown"
    assert C.classify_all([unmeasured], intent=contract).assertions[0].classification != "preserved"

    measured = finding("character.face", "unchanged", confidence="exact",
                       tier="hash", method="content_hash")
    assert C.classify(measured, intent=contract)[0] == "preserved"


def test_an_unmeasured_property_is_not_relabelled_requested_either():
    """The guard is about the claim, not about one word that carries it.

    A `preserved` condition is satisfied by any `unchanged` observation, so a
    classifier that answered the requested question first would report success
    for a row nobody measured -- and `DeltaAssertion.parse` would not object,
    because its refusal is written against `preserved`.
    """
    contract = intent(requested=[{"path": "character.face", "condition": "preserved"}])
    unmeasured = finding("character.face", "unchanged", confidence="unknown", tier="model")
    assert C.classify(unmeasured, intent=contract)[0] not in ("preserved", "requested")


# -- rule 5: coverage is not confidence ------------------------------------


def test_full_coverage_by_a_perceptual_method_does_not_produce_exact():
    everything = COV.build(source_readable=True, target_readable=True,
                           dimensions={"structural": 1.0, "spatial": 1.0})
    assert COV.sufficient(everything, dimensions=("structural", "spatial"))

    seen = finding("character.jacket.color", "modified", confidence="exact",
                   tier="perceptual", method="delta_e")
    assert seen.confidence == "medium"
    assert CONF.propagate(seen.confidence, coverage_ratio=1.0) == "medium"


def test_a_coverage_ratio_can_only_lower_a_confidence():
    assert CONF.propagate("exact", coverage_ratio=1.0) == "exact"
    assert CONF.propagate("exact", coverage_ratio=0.6) == "medium"
    assert CONF.propagate("low", coverage_ratio=1.0) == "low"
    with pytest.raises(DeltaError):
        CONF.propagate("exact", coverage_ratio=1.4)


def test_propagate_does_not_let_exact_through_an_uncertain_alignment():
    """Built by the constructor on purpose: `Alignment.parse` already caps an
    uncertain relation, and this asserts that `propagate` does not depend on
    somebody else having done it."""
    doubtful = Alignment(relation="uncertain", source_element="a", target_element="b",
                         confidence="exact", tier="hash")
    assert CONF.propagate("exact", alignment=doubtful) == "low"

    parsed = Alignment.parse({"relation": "uncertain", "source_element": "a",
                              "target_element": "b", "confidence": "exact",
                              "tier": "hash"})
    assert CONF.propagate("exact", alignment=parsed) == "low"


def test_a_named_limitation_removes_exact_but_not_the_measurement():
    assert CONF.propagate("exact", limitations=("hair detail below resolution",)) == "high"
    assert CONF.propagate("medium", limitations=("hair detail below resolution",)) == "medium"


def test_an_unregistered_method_is_never_promoted_above_its_tier():
    assert CONF.tier_of("content_hash") == "hash"
    assert CONF.tier_of("ssim") == "perceptual"
    assert CONF.tier_of("parser") == "parser"
    assert CONF.tier_of("a_method_nobody_registered") == "model"

    guess = finding("x", "modified", confidence="exact", method="a_method_nobody_registered",
                    tier=CONF.tier_of("a_method_nobody_registered"))
    assert guess.confidence == "medium"


def test_every_confidence_word_has_a_sentence_and_unknown_does_not_reassure():
    for level in CONFIDENCE:
        assert CONF.describe(level).strip()
    assert CONF.describe("a-word-from-nowhere") == CONF.describe("unknown")


# -- coverage --------------------------------------------------------------


def test_merging_coverage_takes_the_minimum_per_dimension_and_the_and_of_readable():
    first = COV.build(source_readable=True, target_readable=True,
                      dimensions={"structural": 1.0, "semantic": 0.9},
                      regions=["body"])
    second = COV.build(source_readable=True, target_readable=False,
                       dimensions={"structural": 0.4}, regions=["body", "head"])
    merged = COV.merge([first, second])

    assert merged.ratio("structural") == 0.4
    assert merged.source_readable is True
    assert merged.target_readable is False
    assert set(merged.regions_analyzed) == {"body", "head"}


def test_a_dimension_only_one_part_measured_is_not_averaged_down_to_zero():
    """`None` and `0.0` are different answers and a merge must not fold them.

    An adapter that never looks at `temporal` is not a witness that no time was
    covered, and treating its silence as a zero would erase the distinction
    `Coverage.ratio` exists to keep.
    """
    measured = COV.build(source_readable=True, target_readable=True,
                         dimensions={"semantic": 0.9})
    silent = COV.build(source_readable=True, target_readable=True,
                       dimensions={"structural": 1.0})
    merged = COV.merge([measured, silent])
    assert merged.ratio("semantic") == 0.9
    assert merged.ratio("temporal") is None


def test_merging_nothing_does_not_report_that_everything_was_compared():
    assert COV.merge([]).both_readable is False


def test_a_dimension_nobody_measured_is_never_sufficient():
    partial = COV.build(source_readable=True, target_readable=True,
                        dimensions={"structural": 1.0})
    assert COV.sufficient(partial, dimensions=("structural",))
    assert not COV.sufficient(partial, dimensions=("semantic",))

    half_read = COV.build(source_readable=True, target_readable=False,
                          dimensions={"structural": 1.0})
    assert not COV.sufficient(half_read, dimensions=("structural",))

    with pytest.raises(DeltaError):
        COV.sufficient(partial, dimensions=("visual",))


def test_gaps_name_the_end_that_could_not_be_read_and_the_axis_that_fell_short():
    thin = COV.build(source_readable=False, target_readable=True,
                     dimensions={"semantic": 0.4}, excluded=["metadata_private"],
                     notes=["keyframe sampling at 1fps"])
    phrases = " | ".join(COV.gaps(thin))
    assert "source" in phrases
    assert "semantic" in phrases
    assert "metadata_private" in phrases
    assert "keyframe sampling at 1fps" in phrases


# -- alignment -------------------------------------------------------------


def test_a_rename_is_moved_and_not_a_missing_plus_an_added():
    body = digest("payload")
    source = snapshot("s", [element("src/old_name.py", body)])
    target = snapshot("t", [element("src/new_name.py", body)])

    result = A.align(source, target)
    moved = result.by_relation("moved")
    assert len(moved) == 1
    left, right, link = moved[0]
    assert (left.key, right.key) == ("src/old_name.py", "src/new_name.py")
    assert link.confidence == "exact"
    assert link.tier == "hash"
    assert not result.by_relation("missing")
    assert not result.by_relation("added")


def test_two_identical_elements_on_each_side_produce_no_invented_pairing():
    body = digest("identical")
    source = snapshot("s", [element("a/1.png", body), element("a/2.png", body)])
    target = snapshot("t", [element("b/1.png", body), element("b/2.png", body)])

    result = A.align(source, target)
    assert not result.by_relation("moved")
    for left, right, link in result.pairs:
        assert link.relation == "uncertain"
        assert link.confidence == "low"
        assert left is None or right is None


def test_a_matching_key_outranks_a_matching_hash():
    """The ladder runs in order: an address that matched is never re-decided."""
    body = digest("shared")
    source = snapshot("s", [element("same.py", body)])
    target = snapshot("t", [element("same.py", body)])
    result = A.align(source, target)
    assert [link.relation for _, _, link in result.pairs] == ["same"]


def test_a_name_resemblance_never_claims_more_than_low():
    source = snapshot("s", [element("report_final_v1.md")])
    target = snapshot("t", [element("report_final_v2.md")])
    result = A.align(source, target)

    guesses = result.by_relation("uncertain")
    assert len(guesses) == 1
    left, right, link = guesses[0]
    assert (left.key, right.key) == ("report_final_v1.md", "report_final_v2.md")
    assert link.confidence == "low"
    assert link.tier == "algorithm"


def test_a_truncated_alignment_reports_coverage_below_one():
    elements = [element(f"f{i}.py", digest(str(i))) for i in range(4)]
    source = snapshot("s", elements)
    target = snapshot("t", elements)

    cut = A.align(source, target, max_elements=2)
    assert cut.truncated is True
    assert cut.coverage_ratio < 1.0

    whole = A.align(source, target)
    assert whole.truncated is False
    assert whole.coverage_ratio == 1.0


# -- classification --------------------------------------------------------


def test_a_forbidden_path_inside_the_allowed_region_is_still_a_regression():
    """The case a scope with only `allowed` gets wrong, and gets wrong quietly.

    The finding satisfies the request on every other reading: it changed, and
    it changed inside the region the user opened for editing.
    """
    contract = intent(
        requested=[{"path": "src", "condition": "changes"}],
        scope={"allowed": ["src"], "forbidden": ["src/settings.py"]},
    )
    touched = finding("src/settings.py", "modified", confidence="exact", tier="parser")

    classification, severity, _ = C.classify(touched, intent=contract)
    assert classification == "regression"
    assert severity_rank(severity) >= severity_rank("material")

    allowed = finding("src/auth.py", "modified", confidence="exact", tier="parser")
    assert C.classify(allowed, intent=contract)[0] == "requested"


def test_security_outranks_a_perfect_linguistic_match():
    contract = intent(
        requested=[{"path": "workflow.publish", "condition": "changes"}],
        invariants=[{"id": "no_new_network", "class": "permissions", "source": "request",
                     "path": "workflow.publish.permissions"}],
    )
    breach = finding("workflow.publish.permissions.network", "modified",
                     before="false", after="true", confidence="exact", tier="parser")
    broken = InvariantResult.parse({"invariant_id": "no_new_network", "status": "violated",
                                    "severity": "blocking", "confidence": "exact",
                                    "tier": "parser"})

    # Without the ordering this is a textbook `requested`: the path is inside
    # the requested one and the operation is the one that was asked for.
    assert C.satisfies(contract.requested[0], breach) is True

    classification, severity, refs = C.classify(breach, intent=contract,
                                                invariant_results=[broken])
    assert classification == "regression"
    assert severity == "blocking"
    assert refs == ()


def test_required_is_never_inferred_from_a_name():
    """§16: a necessary change needs traceable justification, and a name is not one."""
    contract = intent(requested=[{"path": "auth.session", "condition": "changes"}])

    look_alike = finding("auth.session_timeout", "modified", confidence="high", tier="parser")
    classification, _, refs = C.classify(look_alike, intent=contract)
    assert classification != "required"
    assert refs == ()

    justified = finding("auth.session_timeout", "modified", confidence="high",
                        tier="parser", invariant_refs=("keep_login_alive",))
    assert C.classify(justified, intent=contract)[0] == "required"


def test_a_prefix_covers_a_child_path_and_never_a_shared_substring():
    assert C.covers("a.b", "a.b.c")
    assert C.covers("src", "src/auth.py#consume_state")
    assert C.covers("color", "color")
    assert not C.covers("color", "background_color")
    assert not C.covers("color", "colors")

    change = RequestedChange.parse({"path": "color", "condition": "becomes",
                                    "value": "red"})
    near_miss = finding("background_color", "modified", after="red")
    assert C.satisfies(change, near_miss) is False


def test_a_becomes_is_satisfied_by_the_value_and_not_by_the_change():
    change = RequestedChange.parse({"path": "character.jacket.color",
                                    "condition": "becomes", "value": "red"})
    assert C.satisfies(change, finding("character.jacket.color", "modified", after=" Red "))
    assert not C.satisfies(change, finding("character.jacket.color", "modified", after="blue"))
    assert not C.satisfies(change, finding("character.jacket.color", "unchanged", after="red"))


def test_a_change_outside_a_bounded_scope_is_reported_as_out_of_scope():
    contract = intent(scope={"allowed": ["src/api"]})
    stray = finding("src/db/models.py", "modified", confidence="exact", tier="parser")

    result = C.classify_all([stray], intent=contract)
    assert result.out_of_scope == ("src/db/models.py",)
    assert result.assertions[0].classification == "incidental"
    assert result.assertions[0].severity == "material"


def test_an_uncertain_alignment_is_reported_as_unknown_and_not_as_incidental():
    """`incidental` names a path; an uncertain alignment may hold the wrong one."""
    # A bounded scope that CONTAINS the path, so the fallback answer would be
    # `incidental`: without the relation check this test passes on the weaker
    # confidence alone and stops testing the thing it is named after.
    contract = intent(scope={"allowed": ["b.py"]})
    doubtful = Alignment.parse({"relation": "uncertain", "source_element": "a.py",
                                "target_element": "b.py", "confidence": "low",
                                "tier": "algorithm"})
    guessed = finding("b.py", "modified", confidence="exact", tier="parser",
                      alignment=doubtful)
    assert C.classify(guessed, intent=contract)[0] == "unknown"

    certain = Alignment.parse({"relation": "same", "source_element": "b.py",
                               "target_element": "b.py", "confidence": "exact",
                               "tier": "parser"})
    sure = finding("b.py", "modified", confidence="exact", tier="parser",
                   alignment=certain)
    assert C.classify(sure, intent=contract)[0] == "incidental"


# -- verdict ---------------------------------------------------------------


def test_a_becomes_nobody_satisfied_is_unmet_and_the_assessment_is_mismatched():
    contract = intent(requested=[{"path": "character.jacket.color",
                                  "condition": "becomes", "value": "red"}])

    wrong = finding("character.jacket.color", "modified", before="green", after="blue",
                    confidence="exact", tier="parser")
    missed = C.classify_all([wrong], intent=contract)
    assert missed.unmet == ("character.jacket.color",)
    assert not [a for a in missed.assertions if a.classification == "requested"]
    assert V.assess(assertions=missed.assertions, invariants=(), coverage=full_coverage(),
                    intent=contract, unmet=missed.unmet) == "mismatched"

    right = finding("character.jacket.color", "modified", before="green", after="red",
                    confidence="exact", tier="parser")
    done = C.classify_all([right], intent=contract)
    assert done.unmet == ()
    assert V.assess(assertions=done.assertions, invariants=(), coverage=full_coverage(),
                    intent=contract, unmet=done.unmet) == "matched"


def test_an_unmet_request_beside_a_met_one_is_partial_and_not_mismatched():
    contract = intent(requested=[
        {"path": "a.color", "condition": "becomes", "value": "red"},
        {"path": "b.color", "condition": "becomes", "value": "blue"},
    ])
    got_one = finding("a.color", "modified", after="red", confidence="exact", tier="parser")
    result = C.classify_all([got_one], intent=contract)
    assert result.unmet == ("b.color",)
    assert V.assess(assertions=result.assertions, invariants=(), coverage=full_coverage(),
                    intent=contract, unmet=result.unmet) == "partial"


def test_a_delta_with_no_observations_is_inconclusive_and_never_matched():
    """A delta that looked at nothing is not a delta that found nothing."""
    assert V.assess(assertions=(), invariants=(), coverage=full_coverage()) == "inconclusive"

    unchecked = InvariantResult.parse({"invariant_id": "face", "status": "unknown"})
    assert V.assess(assertions=(), invariants=(unchecked,),
                    coverage=full_coverage()) == "inconclusive"

    contract = intent(requested=[{"path": "a", "condition": "changes"}])
    assert V.assess(assertions=(), invariants=(), coverage=full_coverage(),
                    intent=contract, unmet=("a",)) == "inconclusive"


def test_an_unreadable_end_is_inconclusive_whatever_the_ratios_say():
    blind = COV.build(source_readable=False, target_readable=True,
                      dimensions={"structural": 1.0})
    seen = finding("a", "modified", confidence="exact", tier="parser")
    assertions = C.classify_all([seen], intent=intent()).assertions
    assert V.assess(assertions=assertions, invariants=(), coverage=blind) == "inconclusive"


def test_an_intent_with_unknowns_never_reports_matched():
    """A contract that could not translate part of the request cannot say matched."""
    contract = intent(requested=[{"path": "a", "condition": "changes"}],
                      unknowns=["make it pop"])
    done = finding("a", "modified", confidence="exact", tier="parser")
    result = C.classify_all([done], intent=contract)
    assert result.unmet == ()
    assert V.assess(assertions=result.assertions, invariants=(), coverage=full_coverage(),
                    intent=contract, unmet=result.unmet) == "partial"


def test_a_blocking_violation_outranks_every_achieved_request():
    contract = intent(
        requested=[{"path": "workflow.publish", "condition": "changes"}],
        invariants=[{"id": "no_new_network", "class": "permissions", "source": "request",
                     "path": "workflow.publish.permissions"}],
    )
    change = finding("workflow.publish.retries", "modified", confidence="exact", tier="parser")
    broken = InvariantResult.parse({"invariant_id": "no_new_network", "status": "violated",
                                    "severity": "blocking", "confidence": "exact",
                                    "tier": "parser"})
    result = C.classify_all([change], intent=contract, invariant_results=[broken])
    assert V.assess(assertions=result.assertions, invariants=[broken],
                    coverage=full_coverage(), intent=contract,
                    unmet=result.unmet) == "regressed"


def test_blocking_reasons_name_the_invariant_and_the_path():
    contract = intent(invariants=[{"id": "no_new_network", "class": "permissions",
                                   "source": "request", "path": "workflow.publish.permissions"}])
    breach = finding("workflow.publish.permissions.network", "modified",
                     confidence="exact", tier="parser")
    broken = InvariantResult.parse({"invariant_id": "no_new_network", "status": "violated",
                                    "severity": "blocking", "confidence": "exact",
                                    "tier": "parser"})
    result = C.classify_all([breach], intent=contract, invariant_results=[broken])

    reasons = " | ".join(V.blocking_reasons(assertions=result.assertions,
                                            invariants=[broken]))
    assert "no_new_network" in reasons
    assert "workflow.publish.permissions.network" in reasons


def test_an_axis_the_intent_needed_and_nobody_measured_is_not_matched():
    contract = intent(
        requested=[{"path": "character.jacket.color", "condition": "becomes", "value": "red"}],
        invariants=[{"id": "same_face", "class": "identity", "source": "request",
                     "path": "character.face"}],
    )
    done = finding("character.jacket.color", "modified", after="red",
                   confidence="exact", tier="parser")
    result = C.classify_all([done], intent=contract)

    assert V.needed_dimensions(contract) == ("identity",)
    blind = COV.build(source_readable=True, target_readable=True,
                      dimensions={"structural": 1.0})
    assert V.assess(assertions=result.assertions, invariants=(), coverage=blind,
                    intent=contract, unmet=result.unmet) == "inconclusive"

    measured = COV.build(source_readable=True, target_readable=True,
                         dimensions={"identity": 0.9})
    assert V.assess(assertions=result.assertions, invariants=(), coverage=measured,
                    intent=contract, unmet=result.unmet) == "matched"


def test_explain_says_what_was_achieved_and_what_could_not_be_checked():
    contract = intent(requested=[{"path": "character.jacket.color",
                                  "condition": "becomes", "value": "red"}])
    done = finding("character.jacket.color", "modified", after="red",
                   confidence="exact", tier="parser")
    stray = finding("config.timeout", "modified", before="30", after="5",
                    confidence="exact", tier="parser")
    result = C.classify_all([done, stray], intent=contract)
    thin = COV.build(source_readable=True, target_readable=True,
                     dimensions={"semantic": 0.4}, notes=["hair detail below resolution"])

    sentence = V.explain("partial", assertions=result.assertions, invariants=(),
                         coverage=thin, unmet=result.unmet)
    assert "character.jacket.color" in sentence
    assert "config.timeout" in sentence
    assert "hair detail below resolution" in sentence
    assert sentence.startswith("Assessment: partial.")
