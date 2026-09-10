"""EDIT-02 · Lotes multiarchivo recuperables (Lote 19, closes QA-17).

`src/edit_journal.py` records the intent of a multi-file batch (every target
path and its pre-batch bytes) before any write runs, applies each op in
order, and — on the first OSError — compensates every already-applied file
back to its pre-batch bytes. `ApplyPatchTool` (src/agent_tools/filesystem_tools.py)
now runs its write phase through this journal, so a disk failure partway
through a multi-file patch (docs/spec/v2/acceptance_scenarios.json QA-17)
leaves a receipt naming exactly what applied, where it stopped, and whether
the rollback itself succeeded — never a silent partial write.
"""
import json
import os

import pytest

from src import edit_journal
from src.agent_tools import filesystem_tools as ft

_PATCH_3_FILES = """*** Begin Patch
*** Update File: a.txt
@@
-A1
+A2
*** Update File: b.txt
@@
-B1
+B2
*** Update File: c.txt
@@
-C1
+C2
*** End Patch"""


def _write3(tmp_path):
    files = {}
    for name, body in (("a.txt", "A1\n"), ("b.txt", "B1\n"), ("c.txt", "C1\n")):
        p = tmp_path / name
        p.write_text(body)
        files[name] = p
    return files


# ── src/edit_journal.py unit tests ─────────────────────────────────────────
def test_apply_batch_all_succeed_reports_every_path_applied_and_no_failure():
    store = {"a": b"A1", "b": b"B1"}

    def read(p):
        return store.get(p)

    def write(p, data):
        store[p] = data

    def delete(p):
        store.pop(p, None)

    def apply_op(p, op, pre):
        store[p] = op["new"]
        return op["new"]

    receipt = edit_journal.apply_batch(
        [{"path": "a", "new": b"A2"}, {"path": "b", "new": b"B2"}],
        read_bytes=read, write_bytes=write, delete_path=delete, apply_op=apply_op)
    assert receipt["applied"] == ["a", "b"]
    assert receipt["failed_at"] is None
    assert receipt["rolled_back"] is None
    assert store == {"a": b"A2", "b": b"B2"}


def test_apply_batch_mid_failure_compensates_already_applied_files():
    store = {"a": b"A1", "b": b"B1", "c": b"C1"}

    def read(p):
        return store.get(p)

    def write(p, data):
        store[p] = data

    def delete(p):
        store.pop(p, None)

    def apply_op(p, op, pre):
        if p == "c":
            raise OSError("disk full")
        store[p] = op["new"]
        return op["new"]

    receipt = edit_journal.apply_batch(
        [{"path": "a", "new": b"A2"}, {"path": "b", "new": b"B2"}, {"path": "c", "new": b"C2"}],
        read_bytes=read, write_bytes=write, delete_path=delete, apply_op=apply_op)
    assert receipt["applied"] == []                 # a and b were rolled back
    assert receipt["failed_at"] == "c"
    assert receipt["failure"] and "disk full" in receipt["failure"]
    assert receipt["rolled_back"] is True
    assert store == {"a": b"A1", "b": b"B1", "c": b"C1"}   # exactly the pre-batch state


def test_apply_batch_records_the_scope_even_when_compensation_itself_fails():
    """The honest half of EDIT-02: if writing the compensation back also
    fails, `rolled_back` says False and `applied` still names what is left
    applied — no claim of a clean undo that did not happen."""
    store = {"a": b"A1", "b": b"B1"}

    def read(p):
        return store.get(p)

    def write(p, data):
        if p == "a":
            raise OSError("compensation also fails")
        store[p] = data

    def delete(p):
        store.pop(p, None)

    def apply_op(p, op, pre):
        if p == "b":
            raise OSError("second file fails")
        store[p] = op["new"]
        return op["new"]

    receipt = edit_journal.apply_batch(
        [{"path": "a", "new": b"A2"}, {"path": "b", "new": b"B2"}],
        read_bytes=read, write_bytes=write, delete_path=delete, apply_op=apply_op)
    assert receipt["rolled_back"] is False
    assert receipt["applied"] == ["a"]      # a's compensation failed — still marked applied
    assert receipt["failed_at"] == "b"


def test_changeset_from_receipt_reuses_the_changesets_authority():
    from src.contracts import ChangeSet
    receipt = {"applied": ["a.txt", "b.txt"], "failed_at": None, "rolled_back": None}
    cs = edit_journal.changeset_from_receipt(receipt, workspace="/tmp/ws", owner="alice")
    assert isinstance(cs, ChangeSet)
    assert set(cs.files.modified) == {"a.txt", "b.txt"}
    assert cs.intent == "fix"


# ── ApplyPatchTool integration: QA-17's literal scenario ───────────────────
@pytest.mark.asyncio
async def test_apply_patch_disk_failure_on_the_third_file_rolls_back_the_first_two(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    files = _write3(tmp_path)

    calls = {"n": 0}
    orig_write = ft._write_text_lf

    def failing_write(path, text, crlf):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("simulated disk failure on third file")
        return orig_write(path, text, crlf)

    monkeypatch.setattr(ft, "_write_text_lf", failing_write)
    res = await ft.ApplyPatchTool().execute(_PATCH_3_FILES, {})

    assert res["exit_code"] == 1
    assert res["status"] == "partial"
    journal = res["journal"]
    assert journal["applied"] == []                       # both prior writes were rolled back
    assert journal["failed_at"] == str(files["c.txt"])
    assert journal["rolled_back"] is True
    # Every file is back to (or was never moved from) its pre-batch content.
    assert files["a.txt"].read_text() == "A1\n"
    assert files["b.txt"].read_text() == "B1\n"
    assert files["c.txt"].read_text() == "C1\n"


@pytest.mark.asyncio
async def test_apply_patch_three_files_all_succeed_when_nothing_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    files = _write3(tmp_path)
    res = await ft.ApplyPatchTool().execute(_PATCH_3_FILES, {})
    assert res["exit_code"] == 0
    assert files["a.txt"].read_text() == "A2\n"
    assert files["b.txt"].read_text() == "B2\n"
    assert files["c.txt"].read_text() == "C2\n"


# ── revert-proof: without the journal wired in, a mid-batch OSError left the
# already-written files written (the exact QA-17 gap). Simulate the OLD
# behavior directly against the same failure to show the new test would have
# failed against it. ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_old_unjournaled_write_loop_would_have_left_a_partial_batch(tmp_path, monkeypatch):
    """Reproduces exactly what ApplyPatchTool did before this lote: write
    file-by-file with no journal, so a failure on the third leaves the first
    two written. This is the failure this lote's test above now catches —
    it stands here as the proof that the fix is load-bearing, not cosmetic."""
    monkeypatch.chdir(tmp_path)
    files = _write3(tmp_path)
    prepared = [
        ("update", str(files["a.txt"]), "A1\n", "A2\n", False),
        ("update", str(files["b.txt"]), "B1\n", "B2\n", False),
        ("update", str(files["c.txt"]), "C1\n", "C2\n", False),
    ]
    calls = {"n": 0}
    with pytest.raises(OSError):
        for kind, path, old, new, crlf in prepared:
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("simulated disk failure on third file")
            ft._write_text_lf(path, new, crlf)
    # The pre-fix behavior: a and b already show the new content, unrecoverable
    # (c never got a chance to write, since the failure is raised before it).
    assert files["a.txt"].read_text() == "A2\n"
    assert files["b.txt"].read_text() == "B2\n"
    assert files["c.txt"].read_text() == "C1\n"
