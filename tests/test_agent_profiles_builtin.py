"""The ten built-in profiles of §8 (src/agent_profiles/builtin.py).

What is pinned here, and why each one is worth a test rather than a comment:

* **the ten exist, under a slug that can be looked up again.** Every lookup in
  `agent_defs` goes through `clean_slug`, which turns `greedy_builder` into
  `greedy-builder`; a built-in whose slug does not survive that round trip
  loads fine, shows on the page and can never be dispatched;
* **the completion mode is the plan's.** §8 assigns each profile a depth, and
  a drifting default is a silent behaviour change — a `surgeon` that quietly
  became `greedy` is a minimal-diff agent that refactors;
* **no reviewer can write.** §8 says an auditor does not fix things during an
  audit. A prompt asking nicely is not an enforcement point, so the test is on
  the DENY list and on what the allowlist leaves reachable;
* **every profile reference resolves.** A `verification_profile` with a typo
  is a run verified by `default` while its receipt names something stricter;
* **the prompts describe the worker, never an errand** (§3.1). A definition
  outlives every run that used it, so an objective baked into one is wrong from
  the second run onwards;
* **the two that AUGMENT an existing built-in only added fields.** `reviewer`
  and `implementer` already existed; their prompt, tools, denies and permission
  rules must come through byte-identical, or this file is not enriching the
  catalogue, it is quietly rewriting it;
* **nothing that already resolved stopped resolving.** A user definition that
  `extends` one of the augmented built-ins still materialises, and inherits the
  new fields.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.memory.skill_format import parse_frontmatter                # noqa: E402
from src import agent_defs as defs                                        # noqa: E402
from src.agent_profiles import builtin, catalog                           # noqa: E402


#: §8, literally. Not derived from the module under test: a table that reads
#: itself out of the thing it checks cannot catch the thing going wrong.
PLAN_MODES = {
    "surgeon": "literal",
    "builder": "professional",
    "greedy_builder": "greedy",
    "explorer": "maximalist",
    "auditor": "professional",
    "incident_responder": "literal",
    "creative_director": "maximalist",
    "image_artist": "greedy",
    "video_producer": "greedy",
    "night_worker": "maximalist",
}

#: §8's mode for each, same source.
PLAN_AGENT_MODES = {
    "surgeon": "worker",
    "builder": "worker",
    "greedy_builder": "worker",
    "explorer": "coordinator",
    "auditor": "reviewer",
    "incident_responder": "coordinator",
    "creative_director": "coordinator",
    "image_artist": "worker",
    "video_producer": "coordinator",
    "night_worker": "coordinator",
}

_PROFILE_FIELDS = {
    "verification": "verification_profile",
    "context": "context_profile",
    "budget": "budget_profile",
    "collaboration": "collaboration_profile",
    "output": "output_contract",
}


@pytest.fixture(scope="module")
def shipped():
    return builtin.profile_defs()


# ── the ten ────────────────────────────────────────────────────────────────

def test_all_ten_of_the_plan_exist(shipped):
    assert len(shipped) == 10
    for name in builtin.PLAN_SLUGS:
        assert builtin.profile(name) is not None, f"§8 names `{name}` and nothing answers to it"


def test_every_slug_survives_the_lookup_path(shipped):
    """`clean_slug` is on every path that finds a definition by name.

    A slug it rewrites is a definition that loads and can never be dispatched,
    which is why the multi-word profiles ship hyphenated.
    """
    for d in shipped:
        assert defs.clean_slug(d.slug) == d.slug, (
            f"`{d.slug}` is rewritten by clean_slug and would never be found again")


def test_the_plan_names_resolve_to_the_slug_that_ships(shipped):
    # The two that were folded into definitions that already existed.
    assert builtin.profile("surgeon").slug == "implementer"
    assert builtin.profile("auditor").slug == "reviewer"
    # ...and the plan's underscored spelling still finds the hyphenated slug.
    assert builtin.profile("greedy_builder").slug == "greedy-builder"
    assert builtin.profile("night_worker").slug == "night-worker"


def test_no_two_profiles_share_a_slug(shipped):
    slugs = [d.slug for d in shipped]
    assert len(slugs) == len(set(slugs))
    assert builtin.slugs() == tuple(slugs)


def test_nothing_duplicates_a_built_in_that_already_shipped(shipped):
    """A profile with a new slug may not be a rename of one that exists.

    `auditor` next to `reviewer`, both read-only, both reporting and not
    fixing, is the parallel catalogue §31 forbids: two names for one job means
    nobody can answer "which should I have used", and a permission fixed on one
    stays broken on the other.
    """
    existing = {d.slug: d for d in defs.builtins()}
    for d in shipped:
        if d.slug in existing:
            continue
        for old in existing.values():
            same_job = (d.mode == old.mode and set(d.tools) == set(old.tools)
                        and set(d.deny) == set(old.deny))
            assert not same_job, (
                f"`{d.slug}` has the same mode and the same tools as `{old.slug}`; "
                f"extend that definition instead of shipping a twin")


# ── the plan's own values ──────────────────────────────────────────────────

def test_completion_mode_is_the_one_the_plan_assigned():
    for name, mode in PLAN_MODES.items():
        d = builtin.profile(name)
        assert d is not None and d.default_completion_mode == mode, (
            f"§8 gives `{name}` the `{mode}` default; it ships as "
            f"`{d.default_completion_mode if d else 'missing'}`")


def test_agent_mode_is_the_one_the_plan_assigned():
    for name, mode in PLAN_AGENT_MODES.items():
        assert builtin.profile(name).mode == mode


def test_the_capabilities_the_plan_states_are_declared():
    assert set(builtin.profile("surgeon").capabilities) >= {"code"}
    assert set(builtin.profile("builder").capabilities) == {"code", "documents", "automation"}
    assert set(builtin.profile("greedy_builder").capabilities) == {"code", "automation"}
    assert set(builtin.profile("explorer").capabilities) == {"research", "planning"}
    assert set(builtin.profile("auditor").capabilities) == {"review", "security", "testing"}
    assert set(builtin.profile("incident_responder").capabilities) == {
        "code", "automation", "diagnostics"}
    assert set(builtin.profile("creative_director").capabilities) == {
        "image", "video", "audio", "planning", "review"}
    assert set(builtin.profile("image_artist").capabilities) == {"image"}
    assert set(builtin.profile("video_producer").capabilities) == {
        "video", "image", "audio", "documents"}
    # §8 gives night_worker none, and inventing one here would be exactly the
    # self-declared competence §14 refuses to weigh.
    assert builtin.profile("night_worker").capabilities == ()


def test_the_profiles_the_plan_names_are_the_ones_referenced():
    assert builtin.profile("surgeon").verification_profile == "targeted_tests_v1"
    assert builtin.profile("surgeon").context_profile == "narrow_code_v1"
    assert builtin.profile("builder").verification_profile == "full_delivery_v1"
    assert builtin.profile("explorer").collaboration_profile == "exploration_council_v1"
    assert builtin.profile("incident_responder").verification_profile == "incident_containment_v1"
    assert builtin.profile("creative_director").collaboration_profile == "creative_pipeline_v1"
    assert builtin.profile("image_artist").verification_profile == "image_quality_v1"
    assert builtin.profile("night_worker").budget_profile == "overnight_local_v1"
    assert builtin.profile("night_worker").collaboration_profile == "resumable_background_v1"


# ── every reference resolves ───────────────────────────────────────────────

def test_every_profile_reference_exists_in_the_catalogue(shipped):
    """A typo in a profile id is a run verified by `default` whose receipt
    names something stricter. Checked for all ten, across all five kinds."""
    for d in shipped:
        for kind, fieldname in _PROFILE_FIELDS.items():
            value = getattr(d, fieldname)
            if not value:                     # `output_contract` is optional
                continue
            assert catalog.exists(kind, value), (
                f"`{d.slug}` references {kind} profile `{value}`, which the catalogue "
                f"does not have")


def test_all_five_profile_kinds_are_actually_exercised(shipped):
    """Not decoration: if no built-in ever referenced, say, a collaboration
    profile, the test above would pass over a whole family without looking."""
    for kind, fieldname in _PROFILE_FIELDS.items():
        used = {getattr(d, fieldname) for d in shipped if getattr(d, fieldname)}
        assert used, f"no built-in references any {kind} profile"


# ── a reviewer cannot write ────────────────────────────────────────────────

def test_no_reviewer_built_in_can_reach_a_writing_tool(shipped):
    """§8: an auditor does not fix things during an audit.

    Enforced with denies, not with the prompt — `builtin.writes` reports every
    writing tool a definition could still be handed, including the case where
    it stated no allowlist and the session floor would decide.
    """
    reviewers = [d for d in shipped if d.mode == "reviewer"]
    assert reviewers, "no reviewer among the built-ins; this test would pass vacuously"
    for d in reviewers:
        assert builtin.writes(d) == (), (
            f"`{d.slug}` is a reviewer and can still reach {builtin.writes(d)}")
        for tool in builtin.WRITING_TOOLS:
            assert tool in d.deny, (
                f"`{d.slug}` does not DENY `{tool}`; leaving it off the allowlist is not the "
                f"same refusal, because the session floor can put it back")


def test_a_reviewer_that_could_write_is_not_shipped(monkeypatch, caplog):
    """The guarantee above is enforced at build time, not only in this file.

    A reviewer that keeps a shell is dropped with a warning rather than
    shipped: dropping it fails "the ten exist" loudly, whereas shipping it
    would hand a read-only role a way to write.
    """
    broken = dict(builtin.BUILTIN_PROFILES[4])          # the auditor entry
    assert broken["slug"] == "reviewer"
    broken["frontmatter"] = dict(broken["frontmatter"])
    broken["augments"] = ""
    broken["frontmatter"].update({"name": "leaky", "mode": "reviewer",
                                  "tools": ["read_file", "bash"], "deny": []})
    broken["prompt"] = "You review."
    monkeypatch.setattr(builtin, "BUILTIN_PROFILES", (broken,))
    assert builtin.profile_defs() == []


# ── the prompts describe a worker, not an errand (§3.1) ────────────────────

#: Phrases that only ever appear when somebody wrote a RUN into a definition.
_ERRAND_MARKERS = (
    "implement the", "fix the bug", "add the feature", "this repository",
    "our codebase", "the ticket", "step 1", "todo:", "as discussed",
    "the user wants", "for this task, ",
)


def test_no_prompt_carries_the_objective_of_a_run(shipped):
    for d in shipped:
        lowered = d.prompt.lower()
        for marker in _ERRAND_MARKERS:
            assert marker not in lowered, (
                f"`{d.slug}`'s prompt contains {marker!r}: a definition outlives every run "
                f"that used it, so an objective in one is wrong from the second run onwards")


def test_prompts_stay_short_enough_to_read(shipped):
    for d in shipped:
        assert d.prompt.strip(), f"`{d.slug}` ships without a prompt"
        assert len(d.prompt) < 1200, (
            f"`{d.slug}`'s prompt is {len(d.prompt)} chars; a built-in describes a function "
            f"in a few lines, and anything longer is a playbook in the wrong place")


# ── the augmented pair only ADDED ──────────────────────────────────────────

@pytest.mark.parametrize("slug", ["reviewer", "implementer"])
def test_an_augmented_built_in_keeps_everything_it_already_said(slug, shipped):
    """`reviewer` and `implementer` are enriched, not rewritten.

    If any of these drift, the profiles plan stopped extending the catalogue
    and started forking it — which is the failure `builtin.py` exists to
    prevent, one level down from the duplicate-slug case.
    """
    before = {d.slug: d for d in defs.builtins()}[slug]
    after = {d.slug: d for d in shipped}[slug]
    assert after.prompt == before.prompt
    assert after.tools == before.tools
    assert after.deny == before.deny
    assert [r.as_text() for r in after.permission] == [r.as_text() for r in before.permission]
    assert after.mode == before.mode
    assert after.max_rounds == before.max_rounds
    assert after.timeout_s == before.timeout_s
    # ...and it gained exactly what it was missing.
    assert after.capabilities and not before.capabilities
    assert defs.revision_of(after) != defs.revision_of(before)


def test_augmenting_reads_the_shipped_source_rather_than_a_copy():
    """The augmented prompt is the one in `agent_defs.BUILTIN_SOURCES`.

    Copying the text into this package would let the two drift apart with
    nothing failing, which is the quiet half of the duplication problem.
    """
    _, body = parse_frontmatter(defs.BUILTIN_SOURCES["reviewer"])
    assert builtin.profile("auditor").prompt == body.strip()


def test_the_shipped_built_ins_are_not_mutated_by_building_the_profiles():
    """`profile_defs()` reads `BUILTIN_SOURCES`; it must not edit it."""
    before = dict(defs.BUILTIN_SOURCES)
    builtin.profile_defs()
    assert defs.BUILTIN_SOURCES == before
    assert [d.slug for d in defs.builtins()] == ["reviewer", "planner", "implementer"]


# ── what already resolved still resolves ───────────────────────────────────

def test_a_definition_that_extends_an_augmented_built_in_still_materialises():
    """The wiring this file is heading for: the enriched built-ins replacing
    the plain ones in a catalogue, with user definitions on top."""
    catalogue = {d.slug: d for d in defs.builtins()}
    catalogue.update({d.slug: d for d in builtin.profile_defs()})
    child = defs.parse(
        "---\nname: narrow surgeon\nextends: implementer\n"
        "tools: [read_file, ls, grep, edit_file]\nspecialties: [sqlite]\n---\n\n"
        "You work only inside the storage layer.\n",
        slug="narrow-surgeon", source=defs.SOURCE_USER)
    catalogue[child.slug] = child

    result = defs.LoadResult(agents=list(catalogue.values()))
    merged = defs.resolve_extends("narrow-surgeon", defs=result)

    assert merged.inherits == ("implementer",)
    # inherited from the AUGMENTED parent, which is the point of the exercise
    assert merged.default_completion_mode == "literal"
    assert merged.verification_profile == "targeted_tests_v1"
    assert set(merged.capabilities) == {"code"}
    assert "sqlite" in merged.specialties
    # ...and inheritance still only narrows
    assert set(merged.tools) <= set(catalogue["implementer"].tools)


def test_loading_the_profiles_leaves_the_ordinary_catalogue_alone(tmp_path, monkeypatch):
    """A machine with no user definitions still loads exactly what it did."""
    monkeypatch.setattr(defs, "DATA_DIR", str(tmp_path))
    before = defs.load_all()
    builtin.profile_defs()
    after = defs.load_all()
    assert [d.slug for d in before.agents] == [d.slug for d in after.agents]
    assert after.errors == []
