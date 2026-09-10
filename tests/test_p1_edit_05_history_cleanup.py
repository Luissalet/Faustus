"""EDIT-05 — per-file edit history with point-in-time restore, and cleanup
that can only ever reach a file this agent itself registered (never an
arbitrary user file), moving to a recoverable trash instead of deleting.
"""
import asyncio
import os

import pytest

from src.agent_tools import filesystem_tools as ft


# ---- history is populated by the real EditFileTool/WriteFileTool paths ----

def test_write_file_then_edit_file_populate_restorable_history(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.tool_execution._resolve_tool_path",
        lambda raw: str(tmp_path / raw), raising=False)
    target = tmp_path / "notes.md"

    write_tool = ft.WriteFileTool()
    r1 = asyncio.run(write_tool.execute("notes.md\nfirst draft", {}))
    assert r1["exit_code"] == 0

    edit_tool = ft.EditFileTool()
    r2 = asyncio.run(edit_tool.execute(
        '{"path": "notes.md", "old_string": "first draft", "new_string": "second draft"}', {}))
    assert r2["exit_code"] == 0
    assert target.read_text() == "second draft"

    history = ft.list_edit_history(str(target))
    assert [h["tool"] for h in history] == ["write_file", "edit_file"]
    assert history[0]["pre_revision"] is None  # new file — nothing to snapshot
    pre_rev = history[1]["pre_revision"]
    assert pre_rev is not None

    restored = ft.restore_edit_history(str(target), pre_revision=pre_rev)
    assert restored["restored"] is True
    assert target.read_text() == "first draft"


def test_restore_refuses_a_tampered_snapshot(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("v1")
    pre_rev = ft.sha256_revision(b"v1")
    ft.record_edit_history(str(target), tool="edit_file", pre_revision=pre_rev,
                           post_revision="sha256:" + "0" * 64, pre_bytes=b"v1")
    snapshot = tmp_path / ".faustus_edit_history" / (pre_rev.replace("sha256:", "") + ".snapshot")
    assert snapshot.exists()
    snapshot.write_bytes(b"tampered")  # corrupt the recorded snapshot

    result = ft.restore_edit_history(str(target), pre_revision=pre_rev)
    assert result["restored"] is False
    assert "no longer match" in result["reason"]
    assert target.read_text() == "v1"  # untouched


def test_restore_reports_missing_revision_instead_of_guessing(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("v1")
    result = ft.restore_edit_history(str(target), pre_revision="sha256:" + "f" * 64)
    assert result == {"restored": False, "reason": "no snapshot recorded for that revision"}


# ---- cleanup can only reach explicitly registered temp files --------------

def test_cleanup_never_touches_an_unregistered_user_file(tmp_path):
    workspace = str(tmp_path)
    user_file = tmp_path / "user_report.md"
    user_file.write_text("do not touch me")
    temp_file = tmp_path / "scratch.tmp"
    temp_file.write_text("agent scratch output")

    ft.mark_temp_file(workspace, str(temp_file))  # only the temp file is registered
    result = ft.cleanup_temp_files(workspace)

    assert user_file.exists() and user_file.read_text() == "do not touch me"
    assert not temp_file.exists()
    assert result["moved"] == [{"path": "scratch.tmp", "trashed_to": os.path.join(
        result["trash_dir"], "scratch.tmp")}]


def test_cleanup_with_nothing_registered_touches_nothing(tmp_path):
    workspace = str(tmp_path)
    (tmp_path / "anything.md").write_text("still here")
    result = ft.cleanup_temp_files(workspace)
    assert result == {"moved": [], "missing": [], "trash_dir": None}
    assert (tmp_path / "anything.md").exists()


def test_trashed_temp_file_can_be_restored(tmp_path):
    workspace = str(tmp_path)
    temp_file = tmp_path / "work" / "scratch.tmp"
    temp_file.parent.mkdir()
    temp_file.write_text("agent scratch output")
    ft.mark_temp_file(workspace, str(temp_file))

    result = ft.cleanup_temp_files(workspace)
    assert not temp_file.exists()

    restored = ft.restore_trashed_files(result["trash_dir"], workspace)
    assert restored["restored"] == ["work" + os.sep + "scratch.tmp"]
    assert temp_file.read_text() == "agent scratch output"


def test_registry_clears_after_cleanup_so_a_second_run_is_a_no_op(tmp_path):
    workspace = str(tmp_path)
    temp_file = tmp_path / "scratch.tmp"
    temp_file.write_text("x")
    ft.mark_temp_file(workspace, str(temp_file))
    ft.cleanup_temp_files(workspace)

    second = ft.cleanup_temp_files(workspace)
    assert second == {"moved": [], "missing": [], "trash_dir": None}
