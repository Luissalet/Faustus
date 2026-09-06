"""tests/test_delta_engine_intent.py -- freezing a request without guessing.

Every test fixes a RULE and not a snapshot. `DOMAIN_DEFAULTS` will grow, the
recognised condition shapes will grow, and the wording of every description
will change; none of that may let a compiler invent a condition nobody wrote,
let a profile drop a security invariant, or let a revision edit the contract it
supersedes.

The guarantees, one test each:

* a fragment of the request that is not a checkable condition lands in
  `unknowns` word for word, and is never discarded;
* the shapes the plan itself writes -- `path: becomes(x)`, `path: preserved` --
  are recognised, deterministically, and twice over the same input;
* `only` bounds the scope when the caller named the regions, and is an unknown
  when nobody did;
* `revise` does not touch the contract it supersedes, and the new one points
  back at it with a new `frozen_at`;
* security invariants survive an evaluation profile that trims coverage;
* `merge` never lets a weaker source lower a stronger one, in either order;
* a `preserved` request nothing covers comes back as an invariant that says
  there is no method for it, so its result can only be `unknown`;
* a capability field this module does not read is an error, never a silence.
"""

from __future__ import annotations

import pytest

from src.delta_engine import intent as intent_mod
from src.delta_engine import invariants as invariants_mod
from src.delta_engine.contracts import DeltaError, Invariant, ScopeRule

OWNER = "alice"
FROZEN = "2026-09-06T12:00:00Z"
LATER = "2026-09-06T13:30:00Z"


def rival(source: str, severity: str) -> Invariant:
    """One invariant, filed by `source` at `severity`, for a merge collision.

    Class `content` on purpose: `_CLASS_SEVERITY_FLOOR` gives `security`,
    `permissions` and six others a floor that `Invariant.parse` refuses to go
    under, and a merge test needs a class where the severity is genuinely the
    filer's choice -- otherwise the contract, not `merge`, is what keeps the
    severity up, and the test would pass over a broken `merge`.
    """
    return Invariant.parse({
        "id": "document.figures_kept", "class": "content",
        "source": source, "severity": severity,
    })


# -- nothing is guessed, and nothing is dropped ----------------------------


def test_a_fragment_that_is_not_a_condition_is_kept_verbatim_as_an_unknown():
    """Word for word, in `unknowns`, and not turned into a condition.

    Both halves matter. Inventing a condition from prose would give the delta
    an errand the user never set; dropping the prose would let "keep the
    citations" disappear out of the contract and the comparison come back
    `matched` for having ignored it.
    """
    prose = "Make the introduction feel warmer without losing the citations"
    contract = intent_mod.compile(owner=OWNER, domain="document", text=prose,
                                  now=FROZEN)
    assert prose in contract.unknowns
    assert contract.requested == ()
    assert any(prose in line for line in intent_mod.unmet_confirmations(contract))


def test_the_plans_own_condition_shapes_are_recognised_deterministically():
    """`path: becomes(x)` and `path: preserved`, and the same answer twice.

    Determinism is the point of compiling without a model: two runs over one
    request must produce one `IntentContract.fingerprint`, or the §22 cache
    stops recognising the same question and every comparison is recomputed.
    """
    text = ("character.jacket.color: becomes(red)\n"
            "character.pose: preserved")
    first = intent_mod.compile(owner=OWNER, domain="image", text=text, now=FROZEN)
    second = intent_mod.compile(owner=OWNER, domain="image", text=text, now=FROZEN)

    assert [(c.path, c.condition, c.value) for c in first.requested] == [
        ("character.jacket.color", "becomes", "red"),
        ("character.pose", "preserved", ""),
    ]
    assert first.fingerprint() == second.fingerprint()
    assert first.unknowns == ()


def test_becomes_without_a_value_is_an_unknown_and_not_a_condition():
    """A target value nobody named cannot be compared against the result.

    `RequestedChange` refuses it, and the compiler agrees rather than routing
    around the refusal: the fragment is not a condition, so it is an unknown.
    """
    contract = intent_mod.compile(owner=OWNER, domain="image",
                                  text="character.jacket.color: becomes()",
                                  now=FROZEN)
    assert contract.requested == ()
    assert contract.unknowns == ("character.jacket.color: becomes()",)


def test_only_bounds_the_scope_when_the_regions_were_named():
    """§6's example, and the trap in it.

    "Change only the jacket" bounds nothing on its own: with no declared jacket
    mask there is no region to be outside of, and a boundary drawn from a word
    nobody defined would make every other change in the image read as
    incidental. Named regions turn the same word into a note on a scope that
    was already bounded by the caller.
    """
    named = intent_mod.compile(owner=OWNER, domain="image",
                               text="Cambia sólo la chaqueta",
                               scope={"allowed": ["jacket_mask"]}, now=FROZEN)
    assert named.scope.bounded is True
    assert "only" in named.scope.note

    unnamed = intent_mod.compile(owner=OWNER, domain="image",
                                 text="Cambia sólo la chaqueta", now=FROZEN)
    assert unnamed.scope.bounded is False
    assert unnamed.scope.note == ""
    assert unnamed.unknowns == ("Cambia sólo la chaqueta",)


# -- rule 3: a change of mind is a new contract ----------------------------


def test_revise_leaves_the_previous_contract_untouched_and_points_back_at_it():
    """The audit trail keeps both, and the second cannot pretend to be the first.

    Editing the contract after seeing the target is how every evaluation scores
    full marks, so there is no `unfreeze`: the revision is a new object with a
    new id, a new `frozen_at` and a `supersedes` naming the old one, and the old
    one is byte-for-byte what it was.
    """
    previous = intent_mod.compile(
        owner=OWNER, domain="code",
        text="src/auth.py#consume_state: changes",
        acceptance=("the OAuth state is consumed once",), now=FROZEN)
    before = previous.to_dict()

    revised = intent_mod.revise(previous,
                                text="src/auth.py#consume_state: changes\n"
                                     "src/auth.py#timeout: preserved",
                                now=LATER)

    assert previous.to_dict() == before, "revise must not touch the old contract"
    assert revised.supersedes == previous.id
    assert revised.id != previous.id
    assert (previous.frozen_at, revised.frozen_at) == (FROZEN, LATER)
    assert previous.supersedes == ""
    paths = {change.path for change in revised.requested}
    assert "src/auth.py#timeout" in paths


def test_revise_refuses_a_keyword_it_does_not_understand():
    """Rule 2 of `contracts/base.py`: an unknown key is an error, not a default.

    `revise(contract, requsted=[...])` that silently ignored the typo would
    return the old contract's requests under a new id, and nothing downstream
    would say the change never arrived.
    """
    contract = intent_mod.compile(owner=OWNER, domain="code", now=FROZEN)
    with pytest.raises(DeltaError) as raised:
        intent_mod.revise(contract, requsted=[])
    assert "requsted" in str(raised.value)


def test_revise_will_not_let_a_caller_choose_what_it_supersedes():
    """`supersedes` is derived, never passed: a chain must not skip a link."""
    contract = intent_mod.compile(owner=OWNER, domain="code", now=FROZEN)
    with pytest.raises(DeltaError):
        intent_mod.revise(contract, supersedes="intent_somebody_else")


# -- §1.7: a profile buys coverage, never safety ---------------------------


def test_security_invariants_survive_a_literal_profile():
    """A cheaper completion mode drops checks, and never these.

    The second assertion is what keeps the first from being vacuous: the
    `literal` profile has to actually trim something, or the test would pass on
    a profile that does nothing at all.
    """
    security_ids = {item["id"] for item in invariants_mod.SECURITY_INVARIANTS}

    full = invariants_mod.defaults_for("code", profile="default")
    trimmed = invariants_mod.defaults_for("code", profile="literal")

    assert security_ids <= {inv.id for inv in trimmed}
    assert len(trimmed) < len(full), "a literal profile that trims nothing proves nothing"
    assert all(inv.severity == "blocking" for inv in trimmed
               if inv.klass in invariants_mod.MANDATORY_CLASSES)

    contract = intent_mod.compile(owner=OWNER, domain="code",
                                  evaluation_profile="literal", now=FROZEN)
    assert security_ids <= {inv.id for inv in contract.invariants}


def test_an_unrecognised_profile_gets_the_widest_set_and_not_the_narrowest():
    """A profile nobody registered must never be the reason a check was skipped."""
    assert (invariants_mod.defaults_for("code", profile="something-nobody-defined")
            == invariants_mod.defaults_for("code", profile="default"))


# -- merge: a weaker source never lowers a stronger one --------------------


def test_a_project_policy_cannot_lower_an_invariant_the_request_named():
    """The stronger SOURCE wins, and the stronger SEVERITY survives either way.

    Both halves are needed. Source strength alone would let a `request` filing
    the same id at `minor` demote a policy that had it at `blocking`, and §12
    forbids summarising a material row away -- so the demotion would be
    invisible in exactly the delta that needed it.
    """
    request_blocking = rival("request", "blocking")
    policy_minor = rival("project_policy", "minor")

    forwards = invariants_mod.merge([request_blocking], [policy_minor])
    backwards = invariants_mod.merge([policy_minor], [request_blocking])

    for merged in (forwards, backwards):
        assert len(merged) == 1
        assert merged[0].source == "request"
        assert merged[0].severity == "blocking"


def test_a_stronger_source_still_does_not_lower_the_severity():
    """The winner is the stronger source, at the strongest severity in play."""
    merged = invariants_mod.merge([rival("project_policy", "blocking")],
                                  [rival("request", "minor")])
    assert (merged[0].source, merged[0].severity) == ("request", "blocking")


def test_a_domain_default_never_overrides_what_the_caller_asked_for():
    """An explicit invariant is filed as `request`, not left at the parse default.

    Leaving it at `Invariant.parse`'s `domain_default` would put the user's own
    instruction at the bottom of `SOURCE_STRENGTH`, where the domain table
    overrides it.
    """
    resolved = invariants_mod.resolve("code", explicit=[
        {"id": "code.public_symbols_kept", "class": "compatibility",
         "severity": "blocking"},
    ])
    named = [inv for inv in resolved if inv.id == "code.public_symbols_kept"]
    assert len(named) == 1
    assert (named[0].source, named[0].severity) == ("request", "blocking")


# -- §7: unknown invariants come back as rows, not as silence --------------


def test_a_preserved_request_nothing_covers_becomes_an_unmeasurable_invariant():
    """§7: no promise of "everything else identical".

    The empty threshold is the load-bearing part. An invariant with nothing to
    measure against cannot be reported `preserved` by anything honest --
    `InvariantResult.parse` refuses `preserved` with no observation -- so the
    result is `unknown`, which is the answer this module refuses to round up.
    """
    contract = intent_mod.compile(
        owner=OWNER, domain="image",
        requested=[{"path": "character.identity", "condition": "preserved"}],
        now=FROZEN)

    unmeasurable = [inv for inv in contract.invariants
                    if invariants_mod.is_unknown(inv)]
    assert [inv.path for inv in unmeasurable] == ["character.identity"]
    assert unmeasurable[0].threshold == {}
    assert "no method" in invariants_mod.describe(unmeasurable[0]).lower()
    assert any("character.identity" in line
               for line in intent_mod.unmet_confirmations(contract))


def test_an_explicit_invariant_that_covers_the_path_leaves_no_unknown():
    """The unknown row is about absence, so covering the path removes it.

    `classification.covers` is the same prefix rule the classifier uses, so an
    invariant on `character` covers a request about `character.identity` here
    exactly as it does there.
    """
    contract = intent_mod.compile(
        owner=OWNER, domain="image",
        requested=[{"path": "character.identity", "condition": "preserved"}],
        invariants=[{"id": "image.character", "class": "identity",
                     "path": "character", "threshold": {"face_match": 0.9}}],
        now=FROZEN)
    assert [inv for inv in contract.invariants
            if invariants_mod.is_unknown(inv)] == []


def test_a_forbidden_path_gets_an_invariant_so_that_holding_is_evidence():
    """A forbidden path that did not change produces no finding at all.

    Without an invariant the delta has no row saying anybody looked, and
    "we checked settings.py and it held" would be indistinguishable from
    "nothing in this delta mentions settings.py".
    """
    resolved = invariants_mod.resolve(
        "code", scope=ScopeRule.parse({"forbidden": ["src/settings.py"]}))
    guarded = [inv for inv in resolved if inv.klass == "scope"]
    assert [inv.path for inv in guarded] == ["src/settings.py"]
    assert guarded[0].source == "request"


def test_a_capability_field_this_module_does_not_read_is_an_error():
    """A restriction silently ignored is a restriction somebody believed in.

    The delta would come back clean because nothing ever looked, which is the
    most expensive shape of silence in this subsystem.
    """
    with pytest.raises(DeltaError) as raised:
        invariants_mod.resolve("workflow", capability={"permissions": ["network"]})
    assert "permissions" in str(raised.value)


def test_an_unknown_domain_is_refused_by_name():
    """`DOMAINS` is closed: a domain nothing routes on is a string in a database."""
    with pytest.raises(DeltaError):
        invariants_mod.defaults_for("spreadsheet")
