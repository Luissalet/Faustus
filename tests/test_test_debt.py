"""src/test_debt.py — H5, persistent debt journal for tests exempted as
pre-existing/exempt across turns.

Uses a real filesystem (tmp_path, via ODYSSEUS_DATA_DIR / src.constants
monkeypatch) — the module reads/writes DATA_DIR/test_debt/<hash>.json for
real, no fakes."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def test_debt(tmp_path, monkeypatch):
    """A fresh src.test_debt bound to an isolated DATA_DIR for this test."""
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    import src.test_debt as module
    importlib.reload(module)
    monkeypatch.setattr(module, "_DEBT_DIR", str(tmp_path / "test_debt"))
    # No settings module dependency in tests: force defaults deterministically.
    monkeypatch.setattr(module, "_enabled", lambda: True)
    monkeypatch.setattr(module, "_turns_setting", lambda turns: 3 if turns is None else max(1, int(turns)))
    return module


PROJECT = "proj-silhouettes"


def _result(pre_existing=None, exempt=None):
    return {"pre_existing": pre_existing or [], "exempt": exempt or []}


# ── record() ────────────────────────────────────────────────────────────

def test_record_new_test_id_starts_at_one_turn(test_debt):
    res = _result(exempt=["tests/test_e2e.py::test_real_end_to_end[star] — IoU 0.9755 < 0.98"])
    entries = test_debt.record(PROJECT, res, turn=1)
    assert len(entries) == 1
    e = entries[0]
    assert e["test_id"] == "tests/test_e2e.py::test_real_end_to_end[star]"
    assert e["turns_seen"] == 1
    assert e["dismissed"] is False


def test_record_same_turn_twice_is_idempotent():
    pass  # covered by test_record_is_idempotent_within_the_same_turn below


def test_record_is_idempotent_within_the_same_turn(test_debt):
    res = _result(exempt=["tests/test_x.py::test_a — AssertionError"])
    test_debt.record(PROJECT, res, turn=5)
    entries = test_debt.record(PROJECT, res, turn=5)
    assert len(entries) == 1
    assert entries[0]["turns_seen"] == 1


def test_record_across_different_turns_increments(test_debt):
    res = _result(exempt=["tests/test_x.py::test_a — AssertionError"])
    test_debt.record(PROJECT, res, turn=5)
    test_debt.record(PROJECT, res, turn=6)
    entries = test_debt.record(PROJECT, res, turn=7)
    assert entries[0]["turns_seen"] == 3


def test_record_reproduces_the_silhouettes_star_regression_timeline(test_debt):
    """The exact chain from the forensic analysis: chat #13 (14-09), #21
    (15-09), #24 (16-09) — three turns, three days, still 'pre-existing'.
    By the 3rd sighting it must be overdue()."""
    star = "tests/test_e2e.py::test_real_end_to_end[star] — IoU 0.9755 < 0.98"
    test_debt.record(PROJECT, _result(pre_existing=[star], exempt=[star]), turn="2026-09-14T17:25")
    test_debt.record(PROJECT, _result(pre_existing=[star], exempt=[star]), turn="2026-09-15T22:43")
    entries = test_debt.record(PROJECT, _result(pre_existing=[star], exempt=[star]), turn="2026-09-16T08:56")
    assert entries[0]["turns_seen"] == 3
    overdue = test_debt.overdue(PROJECT)
    assert len(overdue) == 1
    assert overdue[0]["test_id"] == "tests/test_e2e.py::test_real_end_to_end[star]"


def test_record_drops_a_test_id_that_stopped_being_reported_fixed(test_debt):
    res = _result(exempt=["tests/test_x.py::test_a — AssertionError"])
    test_debt.record(PROJECT, res, turn=1)
    test_debt.record(PROJECT, res, turn=2)
    fixed = test_debt.record(PROJECT, _result(), turn=3)
    assert fixed == []
    assert test_debt.all_entries(PROJECT) == []


def test_record_a_test_id_reappearing_after_being_dropped_starts_fresh(test_debt):
    res = _result(exempt=["tests/test_x.py::test_a — AssertionError"])
    test_debt.record(PROJECT, res, turn=1)
    test_debt.record(PROJECT, _result(), turn=2)  # fixed, dropped
    entries = test_debt.record(PROJECT, res, turn=3)  # broke again
    assert entries[0]["turns_seen"] == 1


def test_record_merges_exempt_and_pre_existing_without_duplicating(test_debt):
    node = "tests/test_x.py::test_a — AssertionError"
    entries = test_debt.record(PROJECT, _result(pre_existing=[node], exempt=[node]), turn=1)
    assert len(entries) == 1


def test_record_ignores_empty_or_missing_project_id(test_debt):
    assert test_debt.record("", _result(exempt=["x.py::t — e"]), turn=1) == []
    assert test_debt.record(None, _result(exempt=["x.py::t — e"]), turn=1) == []


def test_record_disabled_reads_but_does_not_write(test_debt, monkeypatch):
    monkeypatch.setattr(test_debt, "_enabled", lambda: False)
    res = _result(exempt=["tests/test_x.py::test_a — AssertionError"])
    entries = test_debt.record(PROJECT, res, turn=1)
    assert entries == []
    assert test_debt.all_entries(PROJECT) == []


# ── overdue() ───────────────────────────────────────────────────────────

def test_overdue_empty_before_threshold(test_debt):
    res = _result(exempt=["tests/test_x.py::test_a — AssertionError"])
    test_debt.record(PROJECT, res, turn=1)
    test_debt.record(PROJECT, res, turn=2)
    assert test_debt.overdue(PROJECT) == []


def test_overdue_at_exactly_the_threshold(test_debt):
    res = _result(exempt=["tests/test_x.py::test_a — AssertionError"])
    for t in range(1, 4):
        test_debt.record(PROJECT, res, turn=t)
    assert len(test_debt.overdue(PROJECT)) == 1


def test_overdue_respects_a_custom_turns_argument(test_debt):
    res = _result(exempt=["tests/test_x.py::test_a — AssertionError"])
    for t in range(1, 3):
        test_debt.record(PROJECT, res, turn=t)
    assert test_debt.overdue(PROJECT, turns=2) != []
    assert test_debt.overdue(PROJECT, turns=5) == []


def test_overdue_sorted_most_seen_first(test_debt):
    old = "tests/test_a.py::test_old — E"
    new = "tests/test_b.py::test_new — E"
    for t in range(1, 6):
        test_debt.record(PROJECT, _result(exempt=[old]), turn=f"a{t}")
    for t in range(1, 4):
        test_debt.record(PROJECT, _result(exempt=[old, new]), turn=f"b{t}")
    overdue = test_debt.overdue(PROJECT, turns=3)
    ids = [e["test_id"] for e in overdue]
    assert ids[0].endswith("test_old")


# ── dismiss() ───────────────────────────────────────────────────────────

def test_dismiss_requires_a_reason(test_debt):
    res = _result(exempt=["tests/test_x.py::test_a — AssertionError"])
    test_debt.record(PROJECT, res, turn=1)
    assert test_debt.dismiss(PROJECT, "tests/test_x.py::test_a", "") is False


def test_dismiss_unknown_test_id_returns_false(test_debt):
    assert test_debt.dismiss(PROJECT, "no/such.py::test", "known flaky, tracked in JIRA-1") is False


def test_dismiss_removes_it_from_overdue_and_todo_items(test_debt):
    node = "tests/test_x.py::test_a — AssertionError"
    for t in range(1, 4):
        test_debt.record(PROJECT, _result(exempt=[node]), turn=t)
    assert len(test_debt.overdue(PROJECT)) == 1
    ok = test_debt.dismiss(PROJECT, "tests/test_x.py::test_a", "known flaky, tracked in JIRA-1")
    assert ok is True
    assert test_debt.overdue(PROJECT) == []
    assert test_debt.todo_items(PROJECT) == []


def test_dismiss_is_persistent_across_further_record_calls(test_debt):
    node = "tests/test_x.py::test_a — AssertionError"
    res = _result(exempt=[node])
    for t in range(1, 4):
        test_debt.record(PROJECT, res, turn=t)
    test_debt.dismiss(PROJECT, "tests/test_x.py::test_a", "accepted, will not fix")
    # It keeps failing every subsequent turn; still dismissed, still not overdue.
    for t in range(4, 8):
        test_debt.record(PROJECT, res, turn=t)
    assert test_debt.overdue(PROJECT) == []
    entries = {e["test_id"]: e for e in test_debt.all_entries(PROJECT)}
    dismissed = entries["tests/test_x.py::test_a"]
    assert dismissed["dismissed"] is True
    assert dismissed["dismiss_reason"] == "accepted, will not fix"


def test_dismiss_persists_to_disk_across_a_fresh_load(test_debt):
    node = "tests/test_x.py::test_a — AssertionError"
    test_debt.record(PROJECT, _result(exempt=[node]), turn=1)
    test_debt.dismiss(PROJECT, "tests/test_x.py::test_a", "flaky on CI only")
    entries = test_debt.all_entries(PROJECT)  # re-reads from disk
    assert entries[0]["dismissed"] is True
    assert entries[0]["dismiss_reason"] == "flaky on CI only"


# ── todo_items() ────────────────────────────────────────────────────────

def test_todo_items_shape_is_todowrite_compatible(test_debt):
    node = "tests/test_x.py::test_a — AssertionError"
    for t in range(1, 4):
        test_debt.record(PROJECT, _result(exempt=[node]), turn=t)
    items = test_debt.todo_items(PROJECT)
    assert len(items) == 1
    item = items[0]
    assert item["status"] == "pending"
    assert item["priority"] == "high"
    assert item["content"]
    assert item["id"] == "test_debt:tests/test_x.py::test_a"


def test_todo_items_text_is_bilingual(test_debt):
    node = "tests/test_x.py::test_a — AssertionError"
    for t in range(1, 4):
        test_debt.record(PROJECT, _result(exempt=[node]), turn=t)
    en = test_debt.todo_items(PROJECT, language="en")[0]
    es = test_debt.todo_items(PROJECT, language="es")[0]
    assert "test debt" in en["content"].lower() or "exempt" in en["content"].lower()
    assert "deuda" in es["content"].lower()
    assert en["content_en"] == en["content"]
    assert es["content_es"] == es["content"]


def test_todo_items_empty_when_nothing_overdue(test_debt):
    node = "tests/test_x.py::test_a — AssertionError"
    test_debt.record(PROJECT, _result(exempt=[node]), turn=1)
    assert test_debt.todo_items(PROJECT) == []


def test_todo_items_is_idempotent_across_turns_same_content_and_id(test_debt):
    """CONTRATO.md: 'idempotente entre turnos' — calling todo_items() again
    after another turn where turns_seen has not changed must produce the
    same content/id, so merging never duplicates."""
    node = "tests/test_x.py::test_a — AssertionError"
    for t in range(1, 4):
        test_debt.record(PROJECT, _result(exempt=[node]), turn=t)
    first = test_debt.todo_items(PROJECT)
    second = test_debt.todo_items(PROJECT)
    assert first == second


# ── merge_todo_items() ──────────────────────────────────────────────────

def test_merge_todo_items_adds_new_debt_item_to_existing_list(test_debt):
    existing = [{"content": "write the login test", "status": "pending", "priority": "medium"}]
    debt = [{"id": "test_debt:x.py::t", "content": "Test debt: x.py::t...", "status": "pending", "priority": "high"}]
    merged = test_debt.merge_todo_items(existing, debt)
    assert len(merged) == 2


def test_merge_todo_items_never_duplicates_by_id(test_debt):
    existing = [{"id": "test_debt:x.py::t", "content": "old wording", "status": "pending", "priority": "high"}]
    debt = [{"id": "test_debt:x.py::t", "content": "new wording", "status": "pending", "priority": "high"}]
    merged = test_debt.merge_todo_items(existing, debt)
    assert len(merged) == 1
    assert merged[0]["content"] == "old wording"


def test_merge_todo_items_never_duplicates_by_content_when_ids_are_absent(test_debt):
    """A todo list persisted by coding_tools.TodoWriteTool has no `id` field
    at all — dedup must still work by content."""
    existing = [{"content": "Test debt: x.py::t has been exempt for 3 turns", "status": "pending", "priority": "high"}]
    debt = [{"id": "test_debt:x.py::t", "content": "Test debt: x.py::t has been exempt for 3 turns",
             "status": "pending", "priority": "high"}]
    merged = test_debt.merge_todo_items(existing, debt)
    assert len(merged) == 1


def test_merge_todo_items_idempotent_end_to_end_across_two_simulated_turns(test_debt):
    """Simulate the wiring point: record() -> todo_items() -> merge into the
    project's persisted todos -> next turn does it again -> no duplicate."""
    node = "tests/test_x.py::test_a — AssertionError"
    project_todos = [{"content": "unrelated pending work", "status": "pending", "priority": "medium"}]
    for t in range(1, 5):
        test_debt.record(PROJECT, _result(exempt=[node]), turn=t)
        debt_items = test_debt.todo_items(PROJECT)
        project_todos = test_debt.merge_todo_items(project_todos, debt_items)
    debt_only = [t for t in project_todos if t.get("source") == "test_debt"]
    assert len(debt_only) == 1


# ── project isolation / hashing ─────────────────────────────────────────

def test_two_projects_never_share_a_registry(test_debt):
    node = "tests/test_x.py::test_a — AssertionError"
    test_debt.record("project-A", _result(exempt=[node]), turn=1)
    assert test_debt.all_entries("project-B") == []
    assert len(test_debt.all_entries("project-A")) == 1


def test_project_hash_is_filesystem_safe_for_unusual_ids(test_debt):
    weird_id = "proj/with:chars*??<>|é" * 5
    node = "tests/test_x.py::test_a — AssertionError"
    entries = test_debt.record(weird_id, _result(exempt=[node]), turn=1)
    assert len(entries) == 1
    # Re-reading with the same id resolves to the same file.
    assert test_debt.all_entries(weird_id) == entries
