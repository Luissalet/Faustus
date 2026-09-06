"""
The five profile catalogues, pinned where drift would be silent.

The catalogue is data, so most of what can go wrong with it goes wrong quietly:
an id in a built-in definition that no longer resolves, a context profile
pointing at a Context Engine lane that was renamed, a "reduction" that quietly
hands a task more tokens than its agent was given, a `stricter()` that has the
budget ordering the wrong way round and so answers every override with the
larger budget. None of those raise; all of them are wrong.

So: every id the plan names is checked to exist, every `ContextProfileRef` is
resolved against the *real* `src.context_engine.budgets.PROFILES` rather than
against a copy of its keys, `reduce_budget` is given a patch that asks for more
of everything, and `stricter` is exercised in all five kinds — because the
right answer points in opposite directions depending on the kind.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

import pytest

from src.agent_profiles import catalog, contracts
from src.context_engine import budgets as engine_budgets


@pytest.fixture
def pristine_registry():
    """Undo anything a test registers.

    `register()` mutates module-level dicts by design — that is how a pack adds
    a profile — so a test that adds one has to put the catalogue back or the
    next test is reading someone else's fixtures.
    """
    snapshot = {kind: dict(catalog._REGISTRIES[kind]) for kind in catalog.KINDS}
    yield
    for kind, entries in snapshot.items():
        catalog._REGISTRIES[kind].clear()
        catalog._REGISTRIES[kind].update(entries)


# ── everything the plan names exists ───────────────────────────────────────

_PLANNED = {
    "verification": (
        "default", "targeted_tests_v1", "full_delivery_v1", "security_review_v1",
        "image_quality_v1", "incident_containment_v1", "document_evidence_v1",
    ),
    "context": (
        "default", "narrow_code_v1", "architecture_code_v1",
        "research_primary_sources_v1", "creative_reference_pack_v1",
        "security_review_v1", "incident_v1", "document_evidence_v1",
    ),
    "budget": (
        "default", "fast_local_v1", "standard_v1", "deep_research_v1",
        "creative_preview_v1", "max_quality_v1", "overnight_local_v1",
        "reviewer_standard_v1",
    ),
    "collaboration": (
        "default", "solo_worker_v1", "driver_v1", "navigator_v1",
        "independent_reviewer_v1", "team_coordinator_v1",
        "exploration_council_v1", "creative_pipeline_v1",
        "resumable_background_v1",
    ),
    "output": (
        "review_findings_v1", "implementation_result_v1", "research_report_v1",
    ),
}


@pytest.mark.parametrize("kind, profile_id", [
    (kind, profile_id)
    for kind, ids in _PLANNED.items()
    for profile_id in ids
])
def test_every_profile_the_plan_names_is_in_the_catalogue(kind, profile_id):
    profile = catalog.require(kind, profile_id)
    assert profile.id == profile_id
    assert catalog.exists(kind, profile_id) is True
    assert profile.description.strip(), "a profile nobody can read is a profile nobody picks"


def test_the_kinds_are_the_ones_the_shared_contract_declares():
    assert set(catalog.KINDS) == set(contracts.PROFILE_KINDS)


def test_ids_carry_their_version_except_the_one_fallback_name():
    """`default` is the only unversioned id, and only because it is a fallback.

    Everything an AgentDef writes down by choice is versioned in the name, so a
    definition written today keeps meaning what it meant. `default` is what a
    definition gets when it chooses nothing, so it has to stay spelled the same
    across revisions; its `version` field carries the revision instead.
    """
    for kind in catalog.KINDS:
        for entry in catalog.list_profiles(kind):
            assert entry["id"] == "default" or entry["id"].endswith("_v1"), (kind, entry["id"])
            assert entry["version"], (kind, entry["id"])


def test_get_says_none_and_require_says_what_is_available():
    assert catalog.get("verification", "not_a_profile_v9") is None
    assert catalog.exists("verification", "not_a_profile_v9") is False

    with pytest.raises(contracts.ProfileError) as raised:
        catalog.require("verification", "not_a_profile_v9")
    message = str(raised.value)
    for known in _PLANNED["verification"]:
        assert known in message, "the rejection must list what the caller could have meant"


def test_an_unknown_kind_is_rejected_by_name():
    with pytest.raises(contracts.ProfileError) as raised:
        catalog.get("vibes", "default")
    assert "vibes" in str(raised.value)
    for kind in catalog.KINDS:
        assert kind in str(raised.value)


def test_list_profiles_is_sorted_and_complete():
    listed = catalog.list_profiles("budget")
    ids = [entry["id"] for entry in listed]
    assert ids == sorted(ids)
    assert set(ids) == set(_PLANNED["budget"])
    assert all(entry["ignored"] == () for entry in listed), (
        "a catalogue entry has refused nothing; `ignored` belongs to reductions"
    )


# ── the coupling to the Context Engine (§10) ───────────────────────────────

def test_every_context_profile_points_at_a_lane_the_engine_really_has():
    """Resolved against the live registry, not against a copy of its keys.

    This is the reference most likely to rot: the Context Engine owns its
    profiles and may rename or retire one without ever looking in here. If that
    happens, this test is the thing that notices, instead of a run that quietly
    compiles its context with a fallback nobody chose.
    """
    engine = set(engine_budgets.PROFILES)
    assert engine, "the engine registry is empty; the import is wrong, not the catalogue"
    for entry in catalog.list_profiles("context"):
        assert entry["engine_profile_id"] in engine, (
            "context profile {} points at {!r}, which the engine does not "
            "have; engine lanes are {}".format(
                entry["id"], entry["engine_profile_id"], sorted(engine))
        )
        assert entry["budget_tokens"] > 0


# ── reduction only reduces (§11, §15) ──────────────────────────────────────

def test_a_patch_that_asks_for_more_of_everything_is_granted_none_of_it():
    """The whole point of `reduce_budget`, in one test.

    Every field is asked to grow, plus a field that does not exist. Nothing
    moves, nothing raises, and every refusal comes back named — a task that
    over-reached should see what it did not get, not discover it later by
    running out of tokens it thought it had.
    """
    base = catalog.require("budget", "fast_local_v1")
    greedy_patch = {
        "max_tokens": 10_000_000,
        "max_seconds": 99_999,
        "max_rounds": 500,
        "max_tool_calls": 5_000,
        "max_branches": 64,
        "max_retries": 20,
        "allows_network": True,
        "allows_gpu": True,
        "max_unicorns": 3,
    }
    reduced = catalog.reduce_budget(base, greedy_patch)

    for field in dataclass_fields(base):
        if field.name == "ignored":
            continue
        assert getattr(reduced, field.name) == getattr(base, field.name), field.name

    refusals = " | ".join(reduced.ignored)
    for asked in greedy_patch:
        assert asked in refusals, "{} was refused silently".format(asked)
    assert "not a budget field" in refusals


def test_a_patch_that_shrinks_is_applied_and_says_what_it_still_refused():
    base = catalog.require("budget", "standard_v1")
    reduced = catalog.reduce_budget(base, {
        "max_tokens": 8_000,        # a real reduction
        "allows_network": False,    # switching off is allowed
        "max_branches": 99,         # an expansion hiding in a valid field
    })
    assert reduced.max_tokens == 8_000
    assert reduced.allows_network is False
    assert reduced.max_branches == base.max_branches
    assert "max_branches" in " | ".join(reduced.ignored)
    assert reduced.id != base.id, (
        "a reduced budget must not answer to the catalogue id whose numbers "
        "it no longer has"
    )
    assert base.max_tokens == catalog.require("budget", "standard_v1").max_tokens, (
        "the catalogue entry itself must be untouched"
    )


@pytest.mark.parametrize("patch", [{}, None])
def test_a_patch_that_changes_nothing_returns_the_profile_itself(patch):
    base = catalog.require("budget", "reviewer_standard_v1")
    assert catalog.reduce_budget(base, patch) is base


# ── stricter, in five different directions ─────────────────────────────────

@pytest.mark.parametrize("kind, a, b, expected", [
    # verification: more blocking checks wins.
    ("verification", "targeted_tests_v1", "full_delivery_v1", "full_delivery_v1"),
    ("verification", "full_delivery_v1", "targeted_tests_v1", "full_delivery_v1"),
    ("verification", "default", "security_review_v1", "security_review_v1"),
    # context: the narrower window wins. The opposite of "bigger is stricter".
    ("context", "architecture_code_v1", "narrow_code_v1", "narrow_code_v1"),
    ("context", "narrow_code_v1", "architecture_code_v1", "narrow_code_v1"),
    # budget: less of everything wins. Also the opposite of a `max`.
    ("budget", "overnight_local_v1", "fast_local_v1", "fast_local_v1"),
    ("budget", "fast_local_v1", "max_quality_v1", "fast_local_v1"),
    # collaboration: fewer liberties wins.
    ("collaboration", "team_coordinator_v1", "independent_reviewer_v1",
     "independent_reviewer_v1"),
    ("collaboration", "driver_v1", "navigator_v1", "navigator_v1"),
    # output: the shape that tolerates least around its required fields.
    ("output", "implementation_result_v1", "review_findings_v1", "review_findings_v1"),
])
def test_stricter_points_the_right_way_for_each_kind(kind, a, b, expected):
    assert catalog.stricter(kind, a, b) == expected


def test_a_tie_keeps_the_incumbent():
    """§15 lets an override choose verification that is equal or stronger; an
    equal one therefore changes nothing, and `a` is the incumbent."""
    assert catalog.stricter("verification", "default", "default") == "default"
    assert catalog.stricter("budget", "standard_v1", "standard_v1") == "standard_v1"


def test_stricter_needs_two_real_ids(pristine_registry):
    with pytest.raises(contracts.ProfileError):
        catalog.stricter("budget", "standard_v1", "wishful_v1")


def test_a_required_field_count_beats_the_tiebreak(pristine_registry):
    """Registered rather than hard-coded, so the primary key is exercised too:
    every shipped output contract happens to require exactly three fields."""
    demanding = catalog.OutputContract(
        id="exhaustive_report_v1",
        version="v1",
        fields=("answer", "claims", "sources", "method", "limits"),
        required=("answer", "claims", "sources", "method", "limits"),
        description="Five required fields, for the ordering test.",
    )
    catalog.register("output", demanding)
    assert catalog.stricter("output", "review_findings_v1", "exhaustive_report_v1") \
        == "exhaustive_report_v1"


# ── registration ───────────────────────────────────────────────────────────

def test_registering_the_same_id_twice_with_different_content_is_refused(pristine_registry):
    """Two runs whose receipts both say `targeted_tests_v1` were verified the
    same way. Letting the last loader win is how that stops being true."""
    original = catalog.require("verification", "targeted_tests_v1")

    catalog.register("verification", original)  # identical: a no-op, not an error

    weaker = catalog.VerificationProfile(
        id="targeted_tests_v1",
        version="v1",
        checks=("changeset",),
        blocking=(),
        requires_reviewer=False,
        description="The same name, quietly meaning less.",
    )
    with pytest.raises(contracts.ProfileError):
        catalog.register("verification", weaker)
    assert catalog.require("verification", "targeted_tests_v1") == original


def test_registration_refuses_a_profile_that_cannot_mean_what_it_says(pristine_registry):
    with pytest.raises(contracts.ProfileError):
        catalog.register("verification", catalog.VerificationProfile(
            id="ghost_block_v1", version="v1",
            checks=("changeset",), blocking=("a_check_nobody_declared",),
            requires_reviewer=False, description="blocking outside checks",
        ))
    with pytest.raises(contracts.ProfileError):
        catalog.register("output", catalog.OutputContract(
            id="ghost_field_v1", version="v1",
            fields=("answer",), required=("answer", "sources"),
            description="required outside fields",
        ))
    with pytest.raises(contracts.ProfileError):
        catalog.register("collaboration", catalog.CollaborationProfile(
            id="loud_v1", version="v1", may_lead=False, may_delegate=False,
            speak_policy="whenever_it_feels_like_it", write_policy="read_only",
            review_own_work=False, preferred_roles=(), handoff_contract="",
            description="a speak policy the ordering does not know",
        ))
    with pytest.raises(contracts.ProfileError):
        catalog.register("budget", catalog.require("verification", "default"))


# ── a profile is inert (§9) and grants nothing (§12) ───────────────────────

def test_a_verification_profile_declares_checks_and_has_no_way_to_run_one():
    """§9, enforced by the type rather than by a comment.

    `prove` keeps the verdict. This profile is a list of names the runtime
    resolves against the capabilities it actually has, so the type carries no
    callable, no hook, and no public attribute in which one could be parked.
    """
    public = [name for name in dir(catalog.VerificationProfile)
              if not name.startswith("_")]
    assert public == [], "VerificationProfile grew something executable: {}".format(public)

    for profile in catalog.VERIFICATION_PROFILES.values():
        for field in dataclass_fields(profile):
            value = getattr(profile, field.name)
            assert isinstance(value, (str, bool, tuple)), field.name
            assert not callable(value), field.name
            if isinstance(value, tuple):
                assert all(isinstance(item, str) for item in value), field.name


def test_a_collaboration_profile_cannot_carry_a_permission():
    """§12: `may_delegate` describes a role, it does not confer one.

    The effective ability to delegate is still computed from the permission
    envelope and the depth ceiling. This test guards the adjacent mistake — a
    collaboration profile growing a `tools` or `permission` field and becoming
    a second, softer place where authority is decided.
    """
    names = {f.name for f in dataclass_fields(catalog.CollaborationProfile)}
    forbidden = {"tools", "deny", "permission", "permissions", "work_roots",
                 "effects", "permission_rules", "allow"}
    assert not (names & forbidden), names & forbidden


def test_a_budget_ceiling_is_not_a_key():
    """`allows_network` is a ceiling the envelope still has to agree with.

    Nothing here can be asserted about the envelope — this package does not own
    it — so what is pinned is the direction of `reduce_budget`: the switches can
    only ever be turned off from a patch, which is what makes them safe to read
    as "the most this budget tolerates" rather than "what this run may do".
    """
    permissive = catalog.require("budget", "max_quality_v1")
    assert permissive.allows_network is True
    assert catalog.reduce_budget(permissive, {"allows_network": False}).allows_network is False

    restricted = catalog.require("budget", "reviewer_standard_v1")
    assert restricted.allows_network is False
    assert catalog.reduce_budget(restricted, {"allows_network": True}).allows_network is False


def test_every_id_survives_the_shared_id_grammar_including_a_reduced_one():
    """An id has to be writable into a record, not just readable here.

    `src.contracts.base.ident` is the narrow grammar every contract in Faustus
    routes on — ids become paths, URLs and event names. A catalogue id that
    fails it resolves fine and then blows up at the far end, when a resolution
    is serialised; a derived id from `reduce_budget` would do the same, which
    is why its suffix is `.reduced` and not something prettier.
    """
    from src.contracts.base import ident

    for kind in catalog.KINDS:
        for entry in catalog.list_profiles(kind):
            assert ident({"id": entry["id"]}, "id", kind) == entry["id"]

    derived = catalog.reduce_budget(
        catalog.require("budget", "standard_v1"), {"max_tokens": 1_000})
    assert ident({"id": derived.id}, "id", "budget") == derived.id
    assert not catalog.exists("budget", derived.id), (
        "a reduced budget is run-scoped; it must not look like a catalogue entry"
    )
