"""H18: a reviewed edit is refused when an outside writer touched the file after the preview.

The content revision already caught changed bytes. These tests cover the part it
could not: a rewrite that restored the same bytes (new mtime), and the same guard
for a prepared patch.
"""
import json
import os

import pytest

from src import doubt_review, edit_journal, resource_claims as rc, tool_execution
from src.agent_tools import filesystem_tools as tools


@pytest.fixture
def target(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.txt"
    path.write_text("a old", encoding="utf-8")
    counts = {"write": 0}
    monkeypatch.setattr(tool_execution, "_resolve_tool_path", lambda p: p)
    monkeypatch.setattr(doubt_review, "enabled", lambda: True)
    monkeypatch.setattr(doubt_review, "block_mode_enabled", lambda: True)
    monkeypatch.setattr(tools, "record_edit_history", lambda *a, **k: None)
    real_write = tools._write_text_lf

    def write(*a, **kw):
        counts["write"] += 1
        return real_write(*a, **kw)

    monkeypatch.setattr(tools, "_write_text_lf", write)
    return path, counts


def touch_same_bytes(path, seconds=5):
    """What an outside writer that rewrites identical content leaves behind."""
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


def reviewer(monkeypatch, action):
    async def check(ctx, p, raw, diff, **kw):
        action()
        return {"proceed": True, "section": None, "review": {"verdict": "ok"}}
    monkeypatch.setattr(doubt_review, "check_edit", check)


async def invoke(kind, path):
    if kind == "edit":
        args = {"path": str(path), "old_string": "a", "new_string": "A"}
        return await tools.EditFileTool().execute(json.dumps(args), {})
    return await tools.WriteFileTool().execute(json.dumps({"path": str(path), "content": "A old"}), {})


@pytest.mark.parametrize("kind", ["edit", "write"])
async def test_same_bytes_with_a_new_mtime_are_refused_as_an_external_writer(target, monkeypatch, kind):
    path, counts = target
    reviewer(monkeypatch, lambda: touch_same_bytes(path))
    result = await invoke(kind, path)
    assert result["exit_code"] == 1 and result["status"] == "conflict"
    assert result["error_code"] == "EXTERNAL_WRITER_DETECTED" and result["source"] == "external_writer"
    assert result["changed"] == ["mtime_ns"]
    assert result["preview_identity"]["mtime_ns"] != result["current_identity"]["mtime_ns"]
    assert path.read_text(encoding="utf-8") == "a old" and counts["write"] == 0


@pytest.mark.parametrize("kind", ["edit", "write"])
async def test_changed_bytes_keep_the_existing_review_conflict(target, monkeypatch, kind):
    path, counts = target
    reviewer(monkeypatch, lambda: path.write_text("a newer", encoding="utf-8"))
    result = await invoke(kind, path)
    assert result["error_code"] == "REVIEW_PREVIEW_MISMATCH"      # checked first, unchanged
    assert counts["write"] == 0


@pytest.mark.parametrize("kind", ["edit", "write"])
async def test_an_untouched_file_is_written(target, monkeypatch, kind):
    path, counts = target
    reviewer(monkeypatch, lambda: None)
    assert (await invoke(kind, path))["exit_code"] == 0
    assert path.read_text(encoding="utf-8") == "A old" and counts["write"] == 1


@pytest.mark.parametrize("kind", ["edit", "write"])
async def test_the_check_can_be_turned_off(target, monkeypatch, kind):
    from src import settings
    real = settings.get_setting
    monkeypatch.setattr(settings, "get_setting",
                        lambda k, d=None: False if k == "agent_external_writer_check" else real(k, d))
    path, counts = target
    reviewer(monkeypatch, lambda: touch_same_bytes(path))
    assert (await invoke(kind, path))["exit_code"] == 0
    assert counts["write"] == 1


async def test_a_reviewed_creation_is_refused_when_an_identical_file_appears_unobserved(target, monkeypatch):
    path, counts = target
    path.unlink()
    reviewer(monkeypatch, lambda: path.write_text("A old", encoding="utf-8"))
    result = await invoke("write", path)
    assert result["status"] == "conflict" and result["error_code"] in ("REVIEW_PREVIEW_MISMATCH", "EXTERNAL_WRITER_DETECTED")
    assert counts["write"] == 0


async def test_without_an_effective_review_the_guard_stays_unarmed(target, monkeypatch):
    """No review verdict means no approval to invalidate: behaviour is unchanged."""
    path, counts = target

    async def check(ctx, *a, **kw):
        touch_same_bytes(path)
        return {"proceed": True, "section": None, "review": None}

    monkeypatch.setattr(doubt_review, "check_edit", check)
    assert (await invoke("edit", path))["exit_code"] == 0


async def test_prepared_patch_is_refused_when_the_target_was_touched_after_preparation(tmp_path, monkeypatch):
    target = tmp_path / "t.txt"
    target.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(tool_execution, "_resolve_tool_path", lambda p: p)
    monkeypatch.setattr(doubt_review, "enabled", lambda: True)
    monkeypatch.setattr(doubt_review, "block_mode_enabled", lambda: True)
    monkeypatch.setattr(tools, "record_edit_history", lambda *a, **k: None)
    batches = []
    real = edit_journal.apply_batch
    monkeypatch.setattr(edit_journal, "apply_batch", lambda *a, **kw: batches.append(1) or real(*a, **kw))
    reviewer(monkeypatch, lambda: touch_same_bytes(target))
    patch = f"*** Begin Patch\n*** Update File: {target}\n@@\n-old\n+NEW\n*** End Patch"
    result = await tools.ApplyPatchTool().execute(patch, {})
    assert result["error_code"] == "EXTERNAL_WRITER_DETECTED" and result["status"] == "conflict"
    assert result["changed"] == ["mtime_ns"]
    assert batches == [] and target.read_text(encoding="utf-8") == "old\n"


def test_identity_helper_is_silent_when_nothing_was_captured(tmp_path):
    assert tools._identity_conflict("edit_file", str(tmp_path / "x"), None) is None
    f = tmp_path / "f"
    f.write_text("x")
    assert tools._identity_conflict("edit_file", str(f), rc.stat_identity(str(f))) is None
