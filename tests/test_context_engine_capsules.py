"""Tests for `src.context_engine.capsules` — §8 of the Context Engine plan.

The five §22 requirements for capsules are each pinned by name below:
concurrent deltas detect a stale revision, a restart rebuilds the state, broken
references are marked (not deleted), an uncertain effect is not repeated, and
compacting twice does not duplicate decisions or actions.
"""

import pytest

from src.context_engine import capsules, store
from src.context_engine.contracts import ContextCandidate


@pytest.fixture()
def ce_db(tmp_path):
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield tmp_path
    finally:
        store.use_path(None)


def _capsule(**over):
    fields = {"owner": "luis", "project_id": "p1", "objective": "Implement OAuth"}
    fields.update(over)
    return capsules.ensure("run_1", **fields)


# ── contract and lifecycle ─────────────────────────────────────────────────

def test_ensure_creates_once_and_then_loads(ce_db):
    first = _capsule()
    assert first.scope_id == "run_1"
    assert first.revision == 1
    assert first.last_verified_state == "unknown"

    # A worker calling ensure() with its own wording must not rewrite the
    # coordinator's objective.
    again = capsules.ensure("run_1", owner="luis", objective="something else")
    assert again.objective == "Implement OAuth"
    assert capsules.load("run_1", owner="luis") == first


def test_capsule_round_trips_through_its_own_contract(ce_db):
    _capsule()
    capsules.apply_deltas("run_1", [
        {"op": "set_phase", "value": "review"},
        {"op": "record_decision", "value": "DEC-4"},
        {"op": "claim", "value": "src/auth/github.py", "holder": "worker-1"},
        {"op": "add_evidence", "value": "changeset:9f2c"},
    ], actor="builder", owner="luis")

    loaded = capsules.load("run_1", owner="luis")
    assert capsules.Capsule.parse(loaded.to_dict()) == loaded


def test_a_capsule_never_leaks_across_owners(ce_db):
    _capsule()
    assert capsules.load("run_1", owner="someone_else") is None
    with pytest.raises(capsules.CapsuleError):
        capsules.apply_deltas("run_1", [{"op": "set_phase", "value": "act"}],
                              actor="intruder", owner="someone_else")


def test_deltas_on_a_capsule_that_does_not_exist_are_refused(ce_db):
    with pytest.raises(capsules.CapsuleError):
        capsules.apply_deltas("nope", [{"op": "set_phase", "value": "act"}], actor="a")


def test_delete_removes_the_capsule_and_keeps_the_log(ce_db):
    _capsule()
    capsules.apply_deltas("run_1", [{"op": "set_state", "value": "driver finished"}],
                          actor="builder", owner="luis")

    assert capsules.delete("run_1", owner="luis") is True
    assert capsules.load("run_1", owner="luis") is None
    assert capsules.log("run_1")            # append-only means append-only
    assert capsules.delete("run_1", owner="luis") is False


# ── typed deltas ───────────────────────────────────────────────────────────

def test_an_unknown_op_is_rejected_by_name_without_losing_the_batch(ce_db):
    _capsule()

    result = capsules.apply_deltas("run_1", [
        {"op": "add_next_action", "value": "resolve OBJ-17"},
        {"op": "obliterate", "value": "everything"},
        {"op": "record_decision", "value": "DEC-4"},
    ], actor="builder", owner="luis")

    assert [entry["op"] for entry in result["applied"]] == [
        "add_next_action", "record_decision"]
    assert len(result["rejected"]) == 1
    reason = result["rejected"][0]["reason"]
    assert "obliterate" in reason
    assert "set_phase" in reason and "set_verdict" in reason   # the valid ops are named
    assert result["capsule"]["next_actions"] == ["resolve OBJ-17"]
    assert result["capsule"]["decisions"] == ["DEC-4"]


def test_a_malformed_delta_is_rejected_not_guessed_at(ce_db):
    _capsule()

    result = capsules.apply_deltas("run_1", [
        {"op": "add_next_action"},                              # no value
        {"op": "add_next_action", "value": "   "},              # blank value
        {"op": "add_next_action", "value": 17},                 # not a string
        {"op": "add_next_action", "value": "ok", "holdr": "x"},  # typo'd key
        {"op": "set_verdict", "value": "probably"},             # not a verdict
        "not even an object",
    ], actor="builder", owner="luis")

    assert result["applied"] == []
    assert len(result["rejected"]) == 6
    assert "holdr" in result["rejected"][3]["reason"]
    assert "probably" in result["rejected"][4]["reason"]
    assert capsules.load("run_1", owner="luis").revision == 1


def test_completing_an_action_takes_it_off_the_next_list(ce_db):
    _capsule()
    capsules.apply_deltas("run_1", [{"op": "add_next_action", "value": "resolve OBJ-17"}],
                          actor="builder", owner="luis")

    result = capsules.apply_deltas("run_1", [{"op": "complete", "value": "resolve OBJ-17"}],
                                   actor="builder", owner="luis")

    assert result["capsule"]["completed"] == ["resolve OBJ-17"]
    assert result["capsule"]["next_actions"] == []


def test_claims_and_releases_and_questions(ce_db):
    _capsule()
    capsules.apply_deltas("run_1", [
        {"op": "claim", "value": "src/auth/github.py"},          # holder defaults to actor
        {"op": "open_question", "value": "refresh token rotation"},
    ], actor="worker-1", owner="luis")
    loaded = capsules.load("run_1", owner="luis")
    assert loaded.claims == {"src/auth/github.py": "worker-1"}
    assert loaded.open_questions == ("refresh token rotation",)

    capsules.apply_deltas("run_1", [
        {"op": "release", "value": "src/auth/github.py"},
        {"op": "close_question", "value": "Refresh   Token Rotation"},
    ], actor="worker-1", owner="luis")
    loaded = capsules.load("run_1", owner="luis")
    assert loaded.claims == {}
    assert loaded.open_questions == ()


# ── §22: concurrent deltas detect a stale revision ─────────────────────────

def test_concurrent_deltas_detect_a_stale_revision(ce_db):
    base = _capsule()
    capsules.apply_deltas("run_1", [{"op": "set_phase", "value": "act"}],
                          actor="worker-1", expected_revision=base.revision, owner="luis")

    with pytest.raises(capsules.CapsuleConflict) as exc:
        capsules.apply_deltas("run_1", [{"op": "set_phase", "value": "review"}],
                              actor="worker-2", expected_revision=base.revision,
                              owner="luis")

    assert exc.value.revision == base.revision + 1
    assert exc.value.expected == base.revision
    assert isinstance(exc.value, ValueError)
    assert capsules.load("run_1", owner="luis").phase == "act"   # the loser wrote nothing


def test_expected_revision_must_be_a_number(ce_db):
    _capsule()
    with pytest.raises(capsules.CapsuleError):
        capsules.apply_deltas("run_1", [{"op": "set_phase", "value": "act"}],
                              actor="builder", expected_revision="1", owner="luis")


def test_an_idempotent_batch_does_not_move_the_revision(ce_db):
    _capsule()
    first = capsules.apply_deltas("run_1", [{"op": "record_decision", "value": "DEC-4"}],
                                  actor="builder", owner="luis")
    second = capsules.apply_deltas("run_1", [{"op": "record_decision", "value": "DEC-4"}],
                                   actor="builder", owner="luis")

    # Re-recording something already recorded must not invalidate somebody
    # else's expected_revision for nothing.
    assert second["capsule"]["revision"] == first["capsule"]["revision"]
    assert second["applied"][0]["changed"] is False


# ── §22: compacting twice does not duplicate decisions or actions ──────────

def test_compacting_twice_does_not_duplicate_decisions_or_actions(ce_db):
    _capsule()
    batch = [
        {"op": "add_next_action", "value": "resolve OBJ-17"},
        {"op": "record_decision", "value": "DEC-4: refresh tokens rotate weekly"},
        {"op": "add_evidence", "value": "changeset:9f2c"},
        {"op": "complete", "value": "route contract approved"},
    ]
    capsules.apply_deltas("run_1", batch, actor="compactor", owner="luis")
    # The second compaction re-states the same facts with different spacing and
    # casing, which is exactly what a re-summarising model produces.
    capsules.apply_deltas("run_1", [
        {"op": "add_next_action", "value": "Resolve   OBJ-17"},
        {"op": "record_decision", "value": "dec-4: Refresh tokens rotate weekly"},
        {"op": "add_evidence", "value": "changeset:9f2c"},
        {"op": "complete", "value": "Route contract approved"},
    ], actor="compactor", owner="luis")

    loaded = capsules.load("run_1", owner="luis")
    assert loaded.next_actions == ("resolve OBJ-17",)
    assert loaded.decisions == ("DEC-4: refresh tokens rotate weekly",)
    assert loaded.evidence == ("changeset:9f2c",)
    assert loaded.completed == ("route contract approved",)


# ── §22: a restart rebuilds the state ──────────────────────────────────────

def test_a_restart_rebuilds_the_state_from_the_store(ce_db):
    _capsule()
    capsules.apply_deltas("run_1", [
        {"op": "set_phase", "value": "review"},
        {"op": "set_state", "value": "driver finished the first ChangeSet"},
        {"op": "add_next_action", "value": "resolve OBJ-17"},
        {"op": "set_verdict", "value": "partial"},
        {"op": "claim", "value": "src/auth/github.py", "holder": "worker-1"},
        {"op": "add_evidence", "value": "changeset:9f2c", "source_revision": 12},
    ], actor="builder", owner="luis")

    # There is nothing to invalidate: the store opens a new connection per call,
    # so this read is the same read a freshly started process would do.
    revived = capsules.load("run_1", owner="luis")

    assert revived.phase == "review"
    assert revived.current_state == "driver finished the first ChangeSet"
    assert revived.next_actions == ("resolve OBJ-17",)
    assert revived.claims == {"src/auth/github.py": "worker-1"}
    assert revived.source_revision == 12
    assert revived.last_verified_state == "partial"


def test_source_revision_must_be_a_whole_number(ce_db):
    _capsule()
    result = capsules.apply_deltas(
        "run_1", [{"op": "set_phase", "value": "act", "source_revision": "twelve"}],
        actor="builder", owner="luis")
    assert result["applied"] == []
    assert "source_revision" in result["rejected"][0]["reason"]
    assert capsules.load("run_1", owner="luis").phase == "plan"


# ── §22: an uncertain effect is not repeated ───────────────────────────────

@pytest.mark.parametrize("verdict", capsules.UNCERTAIN_STATES)
def test_an_uncertain_effect_is_announced_in_the_render(ce_db, verdict):
    _capsule()
    capsules.apply_deltas("run_1", [
        {"op": "set_verdict", "value": verdict},
        {"op": "add_next_action", "value": "re-run the migration"},
    ], actor="builder", owner="luis")

    rendered = capsules.render(capsules.load("run_1", owner="luis"))

    assert capsules.UNCERTAIN_EFFECT_NOTE in rendered
    assert f'last_verified_state: "{verdict}"' in rendered
    # The warning sits with the value it is about, not at the far end.
    lines = rendered.splitlines()
    assert lines[lines.index(capsules.UNCERTAIN_EFFECT_NOTE) - 1].startswith(
        "last_verified_state:")


def test_a_verified_capsule_carries_no_warning(ce_db):
    _capsule()
    capsules.apply_deltas("run_1", [{"op": "set_verdict", "value": "verified"}],
                          actor="builder", owner="luis")

    rendered = capsules.render(capsules.load("run_1", owner="luis"))

    assert capsules.UNCERTAIN_EFFECT_NOTE not in rendered
    assert capsules.load("run_1", owner="luis").uncertain() is False


# ── §22: broken references are marked, not deleted ─────────────────────────

def test_validate_marks_broken_references_and_deletes_nothing(ce_db, tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "here.py").write_text("ok\n", encoding="utf-8")
    _capsule()
    capsules.apply_deltas("run_1", [
        {"op": "claim", "value": "src/here.py", "holder": "worker-1"},
        {"op": "claim", "value": "src/gone.py", "holder": "worker-2"},
        {"op": "claim", "value": "../../etc/passwd", "holder": "worker-3"},
        {"op": "add_evidence", "value": "file:src/here.py"},
        {"op": "add_evidence", "value": "file:src/vanished.py"},
        {"op": "add_evidence", "value": "changeset:9f2c"},
    ], actor="builder", owner="luis")
    capsule = capsules.load("run_1", owner="luis")

    report = capsules.validate(capsule, workspace=str(workspace))

    assert report["ok"] is False
    assert {c["subject"] for c in report["stale_claims"]} == {"src/gone.py", "../../etc/passwd"}
    assert [e["ref"] for e in report["missing_evidence"]] == ["file:src/vanished.py"]
    # A reference this module does not own is unchecked, not missing.
    assert [u["ref"] for u in report["unchecked"]] == ["changeset:9f2c"]
    # Nothing was removed: the capsule is exactly as it was.
    assert capsules.load("run_1", owner="luis") == capsule


def test_validate_without_a_workspace_checks_nothing_and_says_so(ce_db):
    _capsule()
    capsules.apply_deltas("run_1", [{"op": "claim", "value": "src/here.py"}],
                          actor="worker-1", owner="luis")

    report = capsules.validate(capsules.load("run_1", owner="luis"))

    assert report["ok"] is True
    assert report["stale_claims"] == []
    assert report["unchecked"][0]["reason"] == "no workspace to check against"


def test_validate_is_clean_when_every_reference_still_resolves(ce_db, tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "here.py").write_text("ok\n", encoding="utf-8")
    _capsule()
    capsules.apply_deltas("run_1", [
        {"op": "claim", "value": "src/here.py", "holder": "worker-1"},
        {"op": "add_evidence", "value": "src/here.py"},
    ], actor="builder", owner="luis")

    report = capsules.validate(capsules.load("run_1", owner="luis"),
                               workspace=str(workspace))
    assert report["stale_claims"] == []
    assert report["missing_evidence"] == []
    assert report["unchecked"] == []
    assert report["ok"] is True


# ── the append-only log ────────────────────────────────────────────────────

def test_every_batch_leaves_a_row_saying_who_and_what(ce_db):
    _capsule()
    capsules.apply_deltas("run_1", [
        {"op": "record_decision", "value": "DEC-4"},
        {"op": "obliterate", "value": "everything"},
    ], actor="worker-1", owner="luis")

    entries = capsules.log("run_1")

    assert len(entries) == 1
    entry = entries[0]
    assert entry["actor"] == "worker-1"
    assert entry["ts"]
    assert [a["op"] for a in entry["applied"]] == ["record_decision"]
    assert len(entry["rejected"]) == 1


def test_the_log_is_newest_first_and_bounded(ce_db):
    _capsule()
    for i in range(3):
        capsules.apply_deltas("run_1", [{"op": "add_next_action", "value": f"step {i}"}],
                              actor=f"worker-{i}", owner="luis")

    assert [e["actor"] for e in capsules.log("run_1")] == ["worker-2", "worker-1", "worker-0"]
    assert len(capsules.log("run_1", limit=1)) == 1


# ── rendering and handing the capsule to the compiler ──────────────────────

def test_render_is_compact_and_omits_what_is_empty(ce_db):
    _capsule()
    capsules.apply_deltas("run_1", [
        {"op": "set_phase", "value": "review"},
        {"op": "record_decision", "value": "DEC-4"},
        {"op": "claim", "value": "src\\auth\\github.py", "holder": "worker-1"},
        {"op": "set_verdict", "value": "verified"},
    ], actor="builder", owner="luis")

    rendered = capsules.render(capsules.load("run_1", owner="luis"))

    assert rendered.startswith("schema_version: 1\n")
    assert 'objective: "Implement OAuth"' in rendered
    assert 'phase: "review"' in rendered
    assert "decisions:\n  - \"DEC-4\"" in rendered
    assert "open_questions:" not in rendered          # empty sections cost lines
    assert "next_actions:" not in rendered
    # A Windows path keeps its backslashes instead of becoming an escape.
    assert '"src\\\\auth\\\\github.py": "worker-1"' in rendered


def test_a_capsule_becomes_a_valid_context_candidate(ce_db):
    _capsule()
    capsules.apply_deltas("run_1", [{"op": "set_verdict", "value": "partial"}],
                          actor="builder", owner="luis")

    candidate = capsules.as_candidate(capsules.load("run_1", owner="luis"))

    assert ContextCandidate.parse(candidate.to_dict()) == candidate
    assert candidate.source_type == "capsule"
    assert candidate.source_ref == "capsule:run_1"
    assert candidate.section == "current_state"
    assert candidate.trust_class == "agent_assertion"     # not verified, not proved
    assert candidate.degraded is True
    assert capsules.UNCERTAIN_EFFECT_NOTE in candidate.body


def test_a_verified_capsule_is_offered_as_a_proved_result(ce_db):
    _capsule()
    capsules.apply_deltas("run_1", [{"op": "set_verdict", "value": "verified"}],
                          actor="builder", owner="luis")

    candidate = capsules.as_candidate(capsules.load("run_1", owner="luis"))

    assert candidate.trust_class == "proved"
    assert candidate.authority == "proved_result"
    assert candidate.degraded is False
