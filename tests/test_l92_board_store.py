"""Lote 92 (OBJ-6) -- src.project_board.Store: model, ids, transitions,
ready/blocked, atomic claim, idempotent import.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import project_board  # noqa: E402

PROJECT = "proj-1"
OTHER_PROJECT = "proj-2"


@pytest.fixture()
def store(tmp_path):
    return project_board.Store(tmp_path / "board.sqlite3")


# ---------------------------------------------------------------------------
# derive_key
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("LocalAI", "LOC"),
    ("Writer's Hoard", "WH"),
    ("Faustus", "FAU"),
])
def test_derive_key_examples(name, expected):
    assert project_board.derive_key(name) == expected


def test_key_valid():
    assert project_board.key_valid("FAU")
    assert project_board.key_valid("AB")
    assert not project_board.key_valid("f")       # too short
    assert not project_board.key_valid("TOOLONG")  # too long
    assert not project_board.key_valid("fau")      # lowercase
    assert not project_board.key_valid("FA1")      # digit


# ---------------------------------------------------------------------------
# Ids
# ---------------------------------------------------------------------------
def test_ids_are_sequential_per_project_and_key(store):
    a = store.create_issue(PROJECT, "FAU", type="bug", title="one")
    b = store.create_issue(PROJECT, "FAU", type="bug", title="two")
    assert a["id"] == "FAU-1"
    assert b["id"] == "FAU-2"


def test_ids_are_independent_per_project_when_keys_differ(store):
    a = store.create_issue(PROJECT, "FAU", type="bug", title="in proj 1")
    b = store.create_issue(OTHER_PROJECT, "TASK", type="bug", title="in proj 2")
    assert a["id"] == "FAU-1"
    assert b["id"] == "TASK-1"  # different project, different key, own counter


def test_ids_never_collide_across_projects_sharing_a_key(store):
    # `issues.id` is a global primary key looked up with no project
    # qualifier (board_get(id), not board_get(project, id)), so two projects
    # that never customized their default derived key must still never be
    # handed the same id -- the sequence skips ahead instead of colliding.
    a = store.create_issue(PROJECT, "FAU", type="bug", title="in proj 1")
    b = store.create_issue(OTHER_PROJECT, "FAU", type="bug", title="in proj 2")
    assert a["id"] != b["id"]
    assert a["id"] == "FAU-1"
    assert b["id"] == "FAU-2"
    # and neither store's own view of the other is corrupted
    assert store.get(a["id"])["project_id"] == PROJECT
    assert store.get(b["id"])["project_id"] == OTHER_PROJECT


def test_changing_the_key_never_renumbers_old_ids(store):
    a = store.create_issue(PROJECT, "FAU", type="bug", title="old key")
    b = store.create_issue(PROJECT, "TASK", type="bug", title="new key")
    assert a["id"] == "FAU-1"
    assert b["id"] == "TASK-1"
    # the old issue is untouched
    assert store.get(a["id"])["id"] == "FAU-1"


def test_create_rejects_unknown_type_and_priority(store):
    with pytest.raises(project_board.BoardError):
        store.create_issue(PROJECT, "FAU", type="not-a-type", title="x")
    with pytest.raises(project_board.BoardError):
        store.create_issue(PROJECT, "FAU", type="bug", title="x", priority="P9")


def test_create_requires_a_title(store):
    with pytest.raises(project_board.BoardError):
        store.create_issue(PROJECT, "FAU", type="bug", title="  ")


# ---------------------------------------------------------------------------
# get() detail shape
# ---------------------------------------------------------------------------
def test_get_returns_full_detail(store):
    issue = store.create_issue(PROJECT, "FAU", type="feature", title="Export to CSV",
                                body_md="details", priority="P1", labels=["x"])
    full = store.get(issue["id"])
    assert full["title"] == "Export to CSV"
    assert full["body_md"] == "details"
    assert full["labels"] == ["x"]
    assert full["comments"] == []
    assert full["events"][0]["kind"] == "created"
    assert full["links"] == []
    assert full["refs"] == []
    assert full["blocked_by"] == []


def test_get_missing_issue_returns_none(store):
    assert store.get("FAU-999") is None


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------
def test_open_to_in_progress_to_done(store):
    issue = store.create_issue(PROJECT, "FAU", type="bug", title="x")
    store.update_issue(issue["id"], {"status": "in_progress"})
    updated = store.update_issue(issue["id"], {"status": "done"})
    assert updated["status"] == "done"
    assert updated["closed_at"]
    kinds = [e["kind"] for e in updated["events"]]
    assert "status_change" in kinds


def test_terminal_status_can_only_reopen(store):
    issue = store.create_issue(PROJECT, "FAU", type="bug", title="x")
    store.update_issue(issue["id"], {"status": "wontfix"})
    with pytest.raises(project_board.InvalidTransitionError) as exc:
        store.update_issue(issue["id"], {"status": "blocked"})
    assert exc.value.error_class == "board.invalid_transition"
    # but reopening to open/in_progress is fine, and clears closed_at
    reopened = store.update_issue(issue["id"], {"status": "open"})
    assert reopened["status"] == "open"
    assert reopened["closed_at"] is None


def test_update_unknown_issue_raises_not_found(store):
    with pytest.raises(project_board.NotFoundError):
        store.update_issue("FAU-999", {"status": "done"})


# ---------------------------------------------------------------------------
# ready() / blocked_by
# ---------------------------------------------------------------------------
def test_ready_excludes_blocked_issues(store):
    a = store.create_issue(PROJECT, "FAU", type="bug", title="blocker")
    b = store.create_issue(PROJECT, "FAU", type="feature", title="blocked",
                            links=[{"kind": "blocked_by", "target": a["id"]}])
    ready_ids = [i["id"] for i in store.ready_issues(PROJECT)]
    assert a["id"] in ready_ids
    assert b["id"] not in ready_ids
    # the blocked issue carries its open blocker
    assert store.get(b["id"])["blocked_by"] == [a["id"]]


def test_ready_reappears_once_the_blocker_closes(store):
    a = store.create_issue(PROJECT, "FAU", type="bug", title="blocker")
    b = store.create_issue(PROJECT, "FAU", type="feature", title="blocked",
                            links=[{"kind": "blocked_by", "target": a["id"]}])
    store.update_issue(a["id"], {"status": "done"})
    ready_ids = [i["id"] for i in store.ready_issues(PROJECT)]
    assert b["id"] in ready_ids
    assert store.get(b["id"])["blocked_by"] == []


def test_blocks_link_is_mirrored(store):
    a = store.create_issue(PROJECT, "FAU", type="bug", title="a")
    b = store.create_issue(PROJECT, "FAU", type="bug", title="b")
    store.add_link(a["id"], "blocks", b["id"])
    # a "blocks" b -> b is "blocked_by" a, stored automatically
    b_links = {(ln["kind"], ln["target_issue_id"]) for ln in store.get(b["id"])["links"]}
    assert ("blocked_by", a["id"]) in b_links


def test_ready_orders_by_priority_then_age(store):
    low = store.create_issue(PROJECT, "FAU", type="task", title="low", priority="P3")
    high = store.create_issue(PROJECT, "FAU", type="task", title="high", priority="P0")
    ready = store.ready_issues(PROJECT)
    assert [i["id"] for i in ready] == [high["id"], low["id"]]


# ---------------------------------------------------------------------------
# Atomic claim
# ---------------------------------------------------------------------------
def test_claim_marks_in_progress_and_assigns(store):
    issue = store.create_issue(PROJECT, "FAU", type="bug", title="x")
    claimed = store.claim(issue["id"], "alice")
    assert claimed["status"] == "in_progress"
    assert claimed["assignee"] == "alice"


def test_claim_by_a_second_assignee_is_refused(store):
    issue = store.create_issue(PROJECT, "FAU", type="bug", title="x")
    store.claim(issue["id"], "alice")
    with pytest.raises(project_board.ClaimedError) as exc:
        store.claim(issue["id"], "bob")
    assert exc.value.error_class == "board.claimed"
    assert exc.value.holder == "alice"


def test_claim_by_the_same_assignee_is_idempotent(store):
    issue = store.create_issue(PROJECT, "FAU", type="bug", title="x")
    store.claim(issue["id"], "alice")
    again = store.claim(issue["id"], "alice")  # must not raise
    assert again["assignee"] == "alice"


def test_claim_unknown_issue_raises_not_found(store):
    with pytest.raises(project_board.NotFoundError):
        store.claim("FAU-999", "alice")


# ---------------------------------------------------------------------------
# Comments / refs / delete
# ---------------------------------------------------------------------------
def test_comment_round_trips(store):
    issue = store.create_issue(PROJECT, "FAU", type="bug", title="x")
    store.add_comment(issue["id"], "progress note", author="agent")
    full = store.get(issue["id"])
    assert full["comments"][0]["body_md"] == "progress note"
    assert full["comments"][0]["author"] == "agent"


def test_ref_is_deduplicated(store):
    issue = store.create_issue(PROJECT, "FAU", type="bug", title="x")
    store.add_ref(issue["id"], "commit", "abc123")
    store.add_ref(issue["id"], "commit", "abc123")
    refs = store.get(issue["id"])["refs"]
    assert len(refs) == 1


def test_delete_cascades(store):
    a = store.create_issue(PROJECT, "FAU", type="bug", title="a")
    b = store.create_issue(PROJECT, "FAU", type="bug", title="b")
    store.add_link(a["id"], "relates_to", b["id"])
    store.add_comment(a["id"], "note")
    assert store.delete_issue(a["id"]) is True
    assert store.get(a["id"]) is None
    # the mirrored link on b is gone too
    assert store.get(b["id"])["links"] == []


# ---------------------------------------------------------------------------
# Import idempotence
# ---------------------------------------------------------------------------
def _project_row(workspace, project_id=PROJECT):
    return {"id": project_id, "workspace": str(workspace)}


def test_import_objetivos_and_pendientes_dry_run_then_apply(store, tmp_path):
    (tmp_path / "OBJETIVOS.md").write_text(
        "# Objetivos\n\n"
        "## OBJ-1 · Primera meta\nDetalle uno.\n\n"
        "## OBJ-2 · ~~Meta ya hecha~~\nDetalle dos, completado.\n",
        encoding="utf-8",
    )
    (tmp_path / "PENDIENTES.md").write_text(
        "# Pendientes\n\n"
        "- [ ] tarea abierta\n"
        "- [x] tarea cerrada\n"
        "- ~~tarea tachada~~\n\n"
        "## Reglas\n"
        "- nunca ejecutar dos modelos grandes a la vez\n",
        encoding="utf-8",
    )
    project = _project_row(tmp_path)

    preview = store.import_sources(project, "FAU", sources=["objetivos", "pendientes"], dry_run=True)
    assert preview["created"] == 0
    # 2 objetivos + 3 checklist lines (the rule line is skipped)
    assert len(preview["preview"]) == 5
    assert store.list_issues(PROJECT)[0] == []  # dry-run wrote nothing

    applied = store.import_sources(project, "FAU", sources=["objetivos", "pendientes"], dry_run=False)
    assert applied["created"] == 5
    assert applied["skipped"] == 0

    issues, _ = store.list_issues(PROJECT, limit=50)
    assert len(issues) == 5
    by_title = {i["title"]: i["status"] for i in issues}
    assert by_title["Primera meta"] == "open"
    assert by_title["Meta ya hecha"] == "done"
    assert by_title["tarea abierta"] == "open"
    assert by_title["tarea cerrada"] == "done"
    assert by_title["tarea tachada"] == "done"
    # the permanent rule under "## Reglas" was never turned into a task
    assert not any("modelos grandes" in t for t in by_title)


def test_import_is_idempotent_on_a_second_pass(store, tmp_path):
    (tmp_path / "OBJETIVOS.md").write_text("## OBJ-1 · Meta\nCuerpo.\n", encoding="utf-8")
    project = _project_row(tmp_path)

    first = store.import_sources(project, "FAU", sources=["objetivos"], dry_run=False)
    assert first["created"] == 1

    second = store.import_sources(project, "FAU", sources=["objetivos"], dry_run=False)
    assert second["created"] == 0
    assert second["skipped"] == 1

    issues, _ = store.list_issues(PROJECT, limit=50)
    assert len(issues) == 1  # never duplicated


def test_import_backlog_json_creates_blocked_by_links(store, tmp_path):
    spec_dir = tmp_path / "docs" / "spec" / "v2"
    spec_dir.mkdir(parents=True)
    (spec_dir / "backlog.json").write_text(json.dumps([
        {"id": "BASE-01", "title": "Base item", "priority": "P1", "status": "open", "depends_on": []},
        {"id": "BASE-02", "title": "Depends on base", "priority": "P2", "status": "open",
         "depends_on": ["BASE-01"]},
    ]), encoding="utf-8")
    project = _project_row(tmp_path)

    result = store.import_sources(project, "FAU", sources=["backlog"], dry_run=False)
    assert result["created"] == 2

    issues, _ = store.list_issues(PROJECT, limit=50)
    by_label = {}
    for compact in issues:
        full = store.get(compact["id"])
        for lab in full["labels"]:
            if lab.startswith("spec:"):
                by_label[lab] = full
    dependent = by_label["spec:BASE-02"]
    assert dependent["blocked_by"] == [by_label["spec:BASE-01"]["id"]]
