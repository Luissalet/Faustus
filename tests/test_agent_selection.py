"""Automatic agent selection (src/agent_profiles/selection.py, §14) and packs (§17).

The rules of §14 are all about what a selector must NOT do, so most of what is
pinned here is an absence:

* **a hard filter runs before the ranking and says why it refused.** A
  quarantined agent that merely scores badly gets picked the moment the good
  one is busy; and a rejection with no reason is re-proposed next turn by
  whoever reads the trace;
* **a requested agent is used or explained, never swapped.** A caller who asked
  for the auditor and silently got the builder was not helped, they were
  overruled without being told;
* **a quarantined agent is out of AUTOMATIC selection only** (§1.8). A person
  can still ask for it, and the trace then carries the caveat;
* **reliability is per task.** An agent excellent at refactoring and hopeless
  with images does not have "a success rate", and asking for one without
  naming a task returns the breakdown and `None`;
* **the only success term is observed.** Declaring capabilities and
  specialities gets a definition considered; it never gets it credit;
* **nothing is chosen for its name.** Two equivalent definitions tie, and the
  tie breaks alphabetically and says so — deterministic and explainable, not
  whichever the dict happened to yield first;
* **what is not known is reported as degraded, not as healthy** (§1.3);
* **a pack groups agents and merges nothing** (§17). Resolving one leaves every
  member's permission envelope exactly as it was.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import agent_defs as defs                                        # noqa: E402
from src.agent_profiles import builtin, packs, selection                  # noqa: E402
from src.agent_profiles.contracts import PermissionEnvelope, ProfileError  # noqa: E402
from src.agent_profiles.selection import TaskSpec                         # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """A disposable outcomes file, as tests/test_agent_defs.py does DATA_DIR.

    Autouse: a selection test that reads the developer's real outcome store
    passes or fails depending on what that machine happened to run last week,
    which is the flakiest possible way to test a ranking.
    """
    monkeypatch.setattr(selection, "DATA_DIR", str(tmp_path))
    return tmp_path


def _def(slug, **kw):
    """A definition built directly, so a selection test is not also a parser test."""
    fields = {
        "name": slug, "mode": "worker",
        "tools": ("read_file", "ls", "grep", "write_file", "edit_file"),
        "capabilities": ("code",), "preferred_tasks": ("refactor",),
        "source": defs.SOURCE_BUILTIN,
    }
    fields.update(kw)
    return defs.AgentDef(slug=slug, **fields)


def _reasons(trace):
    return {slug: reason for slug, reason in trace.alternatives_rejected}


# ── hard filters reject, and say why ───────────────────────────────────────

def test_a_hard_filter_rejects_with_a_reason_a_human_can_act_on():
    catalogue = [
        _def("worker-one"),
        _def("a-reviewer", mode="reviewer", tools=("read_file",), capabilities=("review",)),
        _def("no-shell", tools=("read_file", "ls"), capabilities=("code",)),
        _def("wrong-cap", capabilities=("image",)),
    ]
    task = TaskSpec(intent="refactor", mode="worker", required_capabilities=("code",),
                    required_tools=("write_file",))
    survivors, rejected = selection.hard_filters(catalogue, task)

    assert [d.slug for d in survivors] == ["worker-one"]
    reasons = {c.slug: c.rejected for c in rejected}
    assert "`reviewer`" in reasons["a-reviewer"] and "worker" in reasons["a-reviewer"]
    assert "write_file" in reasons["no-shell"]
    assert "`code`" in reasons["wrong-cap"]
    # every rejection carries a sentence, never a bare slug
    assert all(c.rejected.strip() for c in rejected)


def test_a_rejected_candidate_reaches_the_trace_with_its_reason():
    catalogue = [_def("worker-one"), _def("wrong-cap", capabilities=("image",))]
    trace = selection.select(catalogue, TaskSpec(intent="refactor",
                                                 required_capabilities=("code",)))
    assert trace.chosen == "worker-one"
    assert "`code`" in _reasons(trace)["wrong-cap"]


def test_a_losing_candidate_also_says_why_it_lost():
    """Not only the filtered ones: a candidate that was merely outscored gets
    the comparison, so a reader can tell "wrong tool" from "close second"."""
    catalogue = [_def("aaa"), _def("bbb", preferred_tasks=("refactor", "cleanup"),
                                   tags=("refactor",))]
    trace = selection.select(catalogue, TaskSpec(intent="refactor",
                                                 description="cleanup pass"))
    assert trace.chosen == "bbb"
    assert "scored" in _reasons(trace)["aaa"]


def test_an_unknown_required_capability_says_no_agent_could_have_it():
    catalogue = [_def("worker-one")]
    _, rejected = selection.hard_filters(catalogue, TaskSpec(required_capabilities=("telepathy",)))
    assert "not a known capability" in rejected[0].rejected


# ── the caller's choice wins ───────────────────────────────────────────────

def test_a_requested_agent_beats_a_better_scoring_one():
    catalogue = [_def("aaa"), _def("bbb", preferred_tasks=("refactor",), tags=("refactor",))]
    trace = selection.select(catalogue, TaskSpec(intent="refactor", requested_agent="aaa"))
    assert trace.requested == "aaa" and trace.chosen == "aaa"
    assert "requested by the caller" in trace.reason


def test_a_requested_agent_that_fails_a_hard_filter_is_not_swapped_for_another():
    catalogue = [_def("a-reviewer", mode="reviewer", tools=("read_file",),
                      capabilities=("review",)),
                 _def("worker-one")]
    trace = selection.select(catalogue, TaskSpec(intent="refactor", mode="worker",
                                                 requested_agent="a-reviewer"))
    assert trace.requested == "a-reviewer"
    assert trace.chosen == "", "a substitute was chosen behind the caller's back"
    assert "NOT used" in trace.reason and "worker" in trace.reason


def test_a_requested_agent_nobody_has_is_reported_not_guessed():
    trace = selection.select([_def("worker-one")], TaskSpec(requested_agent="ghost"))
    assert trace.chosen == ""
    assert "not a definition this build knows" in trace.reason


def test_the_plan_name_of_a_shipped_built_in_resolves():
    """§8 calls it `auditor`; it ships as `reviewer`. Someone who read the plan
    types the plan's word, and a silent "unknown agent" would be a lie."""
    catalogue = builtin.profile_defs()
    trace = selection.select(catalogue, TaskSpec(intent="audit", requested_agent="auditor"))
    assert trace.chosen == "reviewer"


# ── quarantine: out of automatic selection, not out of use (§1.8) ──────────

def test_a_quarantined_agent_is_never_selected_automatically():
    catalogue = [_def("aaa"), _def("bbb", preferred_tasks=("refactor",), tags=("refactor",))]
    trace = selection.select(catalogue, TaskSpec(intent="refactor"), quarantined=("bbb",))
    assert trace.chosen == "aaa", "the quarantined agent won on score anyway"
    assert "quarantined" in _reasons(trace)["bbb"]


def test_an_unhealthy_agent_is_never_selected_automatically():
    catalogue = [_def("aaa"), _def("bbb", preferred_tasks=("refactor",), tags=("refactor",))]
    trace = selection.select(catalogue, TaskSpec(intent="refactor"), unhealthy=("bbb",))
    assert trace.chosen == "aaa"
    assert "unhealthy" in _reasons(trace)["bbb"]


def test_a_person_may_still_ask_for_a_quarantined_agent_and_gets_a_caveat():
    catalogue = [_def("aaa"), _def("bbb")]
    trace = selection.select(catalogue, TaskSpec(intent="refactor", requested_agent="bbb"),
                             quarantined=("bbb",))
    assert trace.chosen == "bbb"
    assert "caveat:" in trace.reason and "quarantined" in trace.reason


def test_the_caveat_fires_on_the_slug_the_request_resolved_to():
    """`auditor` and `reviewer` are one agent; a caveat that only fires on one
    spelling is a caveat a caller can type around."""
    catalogue = builtin.profile_defs()
    trace = selection.select(catalogue, TaskSpec(intent="audit", requested_agent="auditor"),
                             quarantined=("reviewer",))
    assert trace.chosen == "reviewer"
    assert "caveat:" in trace.reason and "quarantined" in trace.reason


# ── reliability is per task, and only ever observed ────────────────────────

def test_reliability_is_recorded_and_read_per_task(store):
    for _ in range(3):
        selection.record_outcome("bbb", "refactor", ok=True, ref="run_1")
    for _ in range(3):
        selection.record_outcome("bbb", "image_edit", ok=False, ref="run_2")

    good = selection.reliability("bbb", "refactor")
    bad = selection.reliability("bbb", "image_edit")
    assert good["success_rate"] == 1.0 and good["runs"] == 3
    assert bad["success_rate"] == 0.0 and bad["failed"] == 3
    assert good["scope"] == "task" and good["last_ref"] == "run_1"


def test_an_agent_has_no_single_success_rate(store):
    """§14: excellent at refactoring and hopeless with images is not a number."""
    selection.record_outcome("bbb", "refactor", ok=True)
    selection.record_outcome("bbb", "image_edit", ok=False)

    overall = selection.reliability("bbb")
    assert overall["success_rate"] is None
    assert overall["scope"] == "all_tasks"
    assert overall["by_task"]["refactor"]["success_rate"] == 1.0
    assert overall["by_task"]["image_edit"]["success_rate"] == 0.0


def test_observed_outcomes_change_the_choice_for_that_task_only(store):
    catalogue = [_def("aaa"), _def("bbb")]
    for _ in range(3):
        selection.record_outcome("bbb", "refactor", ok=True)
    for _ in range(3):
        selection.record_outcome("bbb", "image_edit", ok=False)

    assert selection.select(catalogue, TaskSpec(intent="refactor")).chosen == "bbb"
    # ...and the same agent does not carry that credit into other work
    assert selection.select(catalogue, TaskSpec(intent="image_edit")).chosen == "aaa"


def test_declaring_more_never_raises_the_success_term():
    """Capabilities and specialities filter and match. They are not evidence.

    A definition that claims every speciality in the book may out-MATCH a
    modest one, but its `verified_success_rate` stays zero until something
    actually happened.
    """
    boastful = _def("aaa", specialties=("python", "rust", "go", "sql", "css"),
                    tags=("expert", "best", "fastest"))
    candidates = selection.rank([boastful], TaskSpec(intent="refactor",
                                                     specialties=("python",)), history={})
    assert candidates[0].parts["verified_success_rate"] == 0.0
    assert candidates[0].parts["specialty_fit"] == 1.0


def test_reliability_without_a_task_intent_counts_for_nothing_in_the_ranking(store):
    """A task that names no intent cannot be scored on per-task reliability,
    and substituting a lifetime average would be the global score §14 rules
    out. It is reported in the trace rather than passed over."""
    for _ in range(3):
        selection.record_outcome("bbb", "refactor", ok=True)
    trace = selection.select([_def("aaa"), _def("bbb")], TaskSpec(description="something"))
    assert trace.chosen == "aaa"                      # the alphabetical tie-break, not the record
    assert "no task intent stated" in trace.reason


def test_selection_works_with_no_history_at_all(store):
    """Day one on a fresh machine: no store, no health map, no registries."""
    assert not os.path.exists(selection.outcomes_path())
    trace = selection.select([_def("aaa"), _def("bbb")], TaskSpec(intent="refactor"))
    assert trace.chosen in {"aaa", "bbb"}
    assert selection.reliability("aaa", "refactor")["runs"] == 0
    assert selection.reliability("aaa", "refactor")["success_rate"] is None


def test_a_corrupt_store_reads_as_no_outcomes_rather_than_failing(store):
    (store / selection.OUTCOMES_FILENAME).write_text("{not json", encoding="utf-8")
    assert selection.reliability("aaa", "refactor")["runs"] == 0
    assert selection.select([_def("aaa")], TaskSpec(intent="refactor")).chosen == "aaa"


def test_record_outcome_never_raises_when_the_store_cannot_be_written(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(selection, "DATA_DIR", str(blocker))
    selection.record_outcome("aaa", "refactor", ok=True)      # must not raise
    assert selection.reliability("aaa", "refactor")["runs"] == 0


# ── nothing is chosen for its name ─────────────────────────────────────────

def test_two_equivalent_definitions_tie_and_the_tie_breaks_alphabetically(store):
    """Determinism is the requirement; alphabetical is just a rule that has it.

    What matters is that the tie is (a) an actual tie in every measured term,
    (b) broken the same way in every process, and (c) SAID — a reader who is
    told "chosen alphabetically" knows nothing measured told them apart.
    """
    twins = [_def("bbb"), _def("aaa")]
    ranked = selection.rank(twins, TaskSpec(intent="refactor"), history={})
    assert ranked[0].score == ranked[1].score
    assert [c.slug for c in ranked] == ["aaa", "bbb"]

    trace = selection.select(twins, TaskSpec(intent="refactor"))
    assert trace.chosen == "aaa"
    assert "alphabetically" in trace.reason
    # and it is stable across repeated calls with the input the other way round
    assert selection.select(list(reversed(twins)), TaskSpec(intent="refactor")).chosen == "aaa"


def test_a_flattering_slug_beats_nothing(store):
    """The one thing a slug may do is break a tie. It may not create one."""
    flatterer = _def("aaa-elite-refactoring-expert", preferred_tasks=(), tags=(),
                     description="the very best at refactoring anything")
    modest = _def("zzz", preferred_tasks=("refactor",), tags=("refactor",))
    trace = selection.select([flatterer, modest], TaskSpec(intent="refactor"))
    assert trace.chosen == "zzz"


def test_a_catalogue_may_be_a_mapping_a_sequence_or_a_load_result():
    """`{slug: definition}` is the shape the resolver and the loader both hold.

    Iterating a mapping yields its KEYS, which have no `slug` attribute and
    would be discarded by every filter here — a selector that mysteriously
    returns nothing. Normalised at the door instead.
    """
    rows = [_def("aaa"), _def("bbb", preferred_tasks=("refactor", "cleanup"))]
    task = TaskSpec(intent="refactor", description="cleanup pass")
    expected = selection.select(rows, task).chosen
    assert expected == "bbb"
    assert selection.select({d.slug: d for d in rows}, task).chosen == expected
    assert selection.select(defs.LoadResult(agents=rows), task).chosen == expected


def test_the_scores_that_decided_reach_the_trace():
    trace = selection.select([_def("aaa")], TaskSpec(intent="refactor"))
    scores = trace.scores_dict()
    assert "total" in scores and "task_fit" in scores
    assert set(scores) - {"total"} == set(selection.WEIGHTS)


# ── what is not known is declared, not invented (§1.3) ─────────────────────

def test_missing_health_is_reported_as_degraded_rather_than_healthy():
    trace = selection.select([_def("aaa")], TaskSpec(intent="refactor"))
    assert "availability unknown, not healthy" in trace.reason


def test_an_agent_absent_from_a_supplied_health_map_is_penalised_not_assumed_well():
    known = _def("aaa")
    unknown = _def("bbb")
    ranked = selection.rank([known, unknown], TaskSpec(intent="refactor"),
                            history={}, availability={"aaa": "healthy"})
    parts = {c.slug: c.parts for c in ranked}
    assert parts["aaa"]["availability"] == 1.0
    assert parts["bbb"]["availability"] == 0.0
    assert parts["bbb"]["degraded_dependency"] == 1.0
    assert ranked[0].slug == "aaa"


def test_an_absent_registry_does_not_reject_anything():
    """No registry handed in is "we did not look", not "nothing is available"."""
    catalogue = [_def("aaa", model="qwen-30b", runner="claude-code")]
    survivors, rejected = selection.hard_filters(catalogue, TaskSpec(intent="refactor"))
    assert [d.slug for d in survivors] == ["aaa"] and rejected == []
    trace = selection.select(catalogue, TaskSpec(intent="refactor"))
    assert "model availability unchecked" in trace.reason


def test_a_model_that_is_not_loaded_is_a_rejection_with_the_model_named():
    catalogue = [_def("aaa", model="qwen-30b")]
    _, rejected = selection.hard_filters(catalogue, TaskSpec(intent="refactor"),
                                         available_models=("llama-8b",))
    assert "qwen-30b" in rejected[0].rejected


# ── packs group; they do not merge (§17) ───────────────────────────────────

def _catalogue():
    index = {d.slug: d for d in defs.builtins()}
    index.update({d.slug: d for d in builtin.profile_defs()})
    return index


def _envelope(d):
    """The permissions a definition carries, in the type the resolver uses."""
    return PermissionEnvelope(tools=d.tools, deny=d.deny,
                              permission_rules=tuple(r.as_text() for r in d.permission))


def test_resolving_a_pack_changes_no_members_permission_envelope():
    index = _catalogue()
    before = {slug: _envelope(d) for slug, d in index.items()}

    resolved = packs.resolve_pack("secure_feature_team", defs=index)
    members = resolved["members"]

    for slug, definition in members.items():
        assert _envelope(definition) == before[slug], (
            f"`{slug}` came out of the pack with different permissions than it went in with")
        assert definition is index[slug], "the pack handed back a copy, which can drift"
    # and the members are still different from each other: nothing was unioned
    assert members["reviewer"].tools != members["builder"].tools
    assert set(members["reviewer"].deny) - set(members["builder"].deny)


def test_a_pack_has_no_field_a_permission_could_be_written_into():
    forbidden = {"tools", "deny", "permission", "permissions", "work_roots", "effects", "files"}
    assert not forbidden & set(packs.PACK_FIELDS)


def test_a_pack_that_tries_to_grant_tools_is_reported_as_such():
    rogue = packs.AgentPack(pack="rogue", version="1.0.0", coordinator="planner",
                            workers=("builder",), reviewer="reviewer",
                            defaults={"tools": ["bash"]}, description="", requires=())
    problems = packs.validate_pack(rogue, defs=_catalogue())
    assert any("never merges their tools or permissions" in p for p in problems)


def test_every_shipped_pack_validates():
    index = _catalogue()
    for name, p in packs.BUILTIN_PACKS.items():
        assert packs.validate_pack(p, defs=index) == [], f"pack `{name}` does not validate"


def test_a_pack_that_names_a_missing_agent_refuses_rather_than_half_resolving():
    with pytest.raises(ProfileError):
        packs.resolve_pack("secure_feature_team", defs={"planner": _def("planner",
                                                                       mode="coordinator")})
    with pytest.raises(ProfileError):
        packs.resolve_pack("no_such_pack")


def test_a_reviewer_in_a_worker_slot_is_a_problem():
    rogue = packs.AgentPack(pack="rogue", version="1.0.0", coordinator="planner",
                            workers=("reviewer",), reviewer="reviewer",
                            defaults={}, description="", requires=())
    problems = packs.validate_pack(rogue, defs=_catalogue())
    assert any("worker slot" in p for p in problems)


def test_resolve_pack_reports_its_own_problems_without_raising():
    resolved = packs.resolve_pack("media_production", defs=_catalogue())
    assert resolved["problems"] == []
    assert resolved["defaults"]["completion_mode"] == "greedy"
    assert [w.slug for w in resolved["workers"]] == ["image-artist", "video-producer"]
