"""
tests/test_p1_write_05_project_adapter.py — WRITE-05, lote 53.

Acceptance line: "Una sincronizacion bidireccional no sobrescribe
ediciones concurrentes ni requiere que el proyecto sea un repositorio Git."
`src/writing_project_adapter.py` never shells out to git or touches a
`.git` directory anywhere — it works purely off two in-memory manifests and
a hash, which this file's own fixtures make explicit.
"""
from __future__ import annotations

from src.writing_project_adapter import (
    ManifestItem,
    ProjectManifest,
    SyncState,
    apply_sync_result,
    content_hash,
    plan_sync,
)


def _item(item_id, text, kind="chapter"):
    return ManifestItem(id=item_id, kind=kind, title=item_id, content_hash=content_hash(text))


def test_a_new_remote_chapter_is_planned_to_pull_and_nothing_conflicts():
    local = ProjectManifest.from_items("p1", [])
    remote = ProjectManifest.from_items("p1", [_item("ch1", "Chapter one.")])
    state = SyncState(project_id="p1")
    plan = plan_sync(local, remote, state)
    assert plan["pull"] == ["ch1"]
    assert plan["push"] == []
    assert plan["conflicts"] == []


def test_a_local_only_chapter_is_planned_to_push():
    local = ProjectManifest.from_items("p1", [_item("ch2", "New local chapter.")])
    remote = ProjectManifest.from_items("p1", [])
    state = SyncState(project_id="p1")
    plan = plan_sync(local, remote, state)
    assert plan["push"] == ["ch2"] and plan["pull"] == []


def test_a_concurrent_edit_on_both_sides_is_a_conflict_not_a_silent_pick():
    """The exact scenario the acceptance line names: both sides changed the
    same item since the last sync. Neither `pull` nor `push` may touch it."""
    original = "Chapter one, draft."
    state = SyncState(project_id="p1", last_synced_hash={"ch1": content_hash(original)})
    local = ProjectManifest.from_items("p1", [_item("ch1", "Chapter one, edited on desktop.")])
    remote = ProjectManifest.from_items("p1", [_item("ch1", "Chapter one, edited on phone.")])

    plan = plan_sync(local, remote, state)
    assert plan["conflicts"] == ["ch1"]
    assert "ch1" not in plan["pull"]
    assert "ch1" not in plan["push"]


def test_first_time_divergence_with_no_prior_sync_is_also_a_conflict():
    """No basis to prefer either side on a first sync — favouring one would
    BE the last-writer-wins bug this requirement forbids."""
    state = SyncState(project_id="p1")   # never synced before
    local = ProjectManifest.from_items("p1", [_item("ch1", "Local original.")])
    remote = ProjectManifest.from_items("p1", [_item("ch1", "Remote original.")])
    plan = plan_sync(local, remote, state)
    assert plan["conflicts"] == ["ch1"]


def test_only_one_side_changed_since_last_sync_is_not_a_conflict():
    original = "Chapter one, draft."
    state = SyncState(project_id="p1", last_synced_hash={"ch1": content_hash(original)})
    local = ProjectManifest.from_items("p1", [_item("ch1", original)])                  # unchanged
    remote = ProjectManifest.from_items("p1", [_item("ch1", "Chapter one, edited on phone.")])
    plan = plan_sync(local, remote, state)
    assert plan["conflicts"] == []
    assert plan["pull"] == ["ch1"]


def test_apply_sync_result_advances_state_but_leaves_conflicts_flagged():
    original = "Chapter one, draft."
    state = SyncState(project_id="p1", last_synced_hash={"ch1": content_hash(original),
                                                          "ch2": content_hash("Chapter two.")})
    local = ProjectManifest.from_items("p1", [
        _item("ch1", "Edited on desktop."), _item("ch2", "Chapter two."),
    ])
    remote = ProjectManifest.from_items("p1", [
        _item("ch1", "Edited on phone."), _item("ch2", "Chapter two."),
    ])
    plan = plan_sync(local, remote, state)
    assert plan["conflicts"] == ["ch1"] and plan["unchanged"] == ["ch2"]

    new_state = apply_sync_result(state, local, remote, plan)
    # The resolved item's hash advances; the conflicted one is left exactly
    # as it was so the NEXT diff still flags it.
    assert new_state.last_synced_hash["ch2"] == content_hash("Chapter two.")
    assert new_state.last_synced_hash["ch1"] == state.last_synced_hash["ch1"]
    replay = plan_sync(local, remote, new_state)
    assert replay["conflicts"] == ["ch1"]


def test_no_git_repository_is_touched_anywhere_in_this_module():
    import inspect
    import src.writing_project_adapter as mod
    source = inspect.getsource(mod)
    for needle in ("subprocess", "import git", ".git", "GitPython", "git.Repo"):
        assert needle not in source, f"found {needle!r} — this module must stay git-free"
