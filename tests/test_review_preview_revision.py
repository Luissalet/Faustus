"""Reviewed previews retain their revision without holding a lock across await."""
import json

import pytest

from src import doubt_review, tool_execution
from src.agent_tools import filesystem_tools as tools


@pytest.fixture
def target(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.txt"
    path.write_text("a old", encoding="utf-8")
    counts = {"write": 0, "history": 0}
    monkeypatch.setattr(tool_execution, "_resolve_tool_path", lambda p: p)
    monkeypatch.setattr(doubt_review, "enabled", lambda: True)
    monkeypatch.setattr(doubt_review, "block_mode_enabled", lambda: True)
    real_write = tools._write_text_lf

    def write(*a, **kw):
        counts["write"] += 1
        return real_write(*a, **kw)

    def history(*a, **kw):
        counts["history"] += 1

    monkeypatch.setattr(tools, "_write_text_lf", write)
    monkeypatch.setattr(tools, "record_edit_history", history)
    return path, counts


def gate(review):
    return {"proceed": True, "section": None, "review": review}


async def invoke(kind, path, ctx=None):
    args = {"path": str(path)}
    if kind == "edit":
        args.update(old_string="a", new_string="A")
        return await tools.EditFileTool().execute(json.dumps(args), ctx or {})
    args["content"] = "A old"
    return await tools.WriteFileTool().execute(json.dumps(args), ctx or {})


def reviewing(monkeypatch, path, result, *, mutate=True):
    seen = []

    async def check(ctx, p, raw, diff, **kwargs):
        if ctx.get("inner"):
            return gate(None)
        seen.append(diff)
        if mutate:
            inner = await tools.WriteFileTool().execute(
                json.dumps({"path": str(path), "content": "a newer"}), {"inner": True})
            assert inner["exit_code"] == 0
        return result

    monkeypatch.setattr(doubt_review, "check_edit", check)
    return seen


@pytest.mark.parametrize("kind", ["edit", "write"])
async def test_changed_review_preview_blocks_outer_write_and_history(target, monkeypatch, kind):
    path, counts = target
    original = tools.sha256_revision(path.read_bytes())
    seen = reviewing(monkeypatch, path, gate({"verdict": "ok", "concerns": []}))

    class UnusedPolicy:
        def observe_lines(self, *a, **kw):
            pytest.fail("obsolete preview reached rewrite policy")

    result = await invoke(kind, path, {"rewrite_policy": UnusedPolicy()})
    assert "-a old" in seen[0]["text"] and "+A old" in seen[0]["text"]
    assert result["exit_code"] == 1 and result["status"] == "conflict"
    assert result["error_code"] == "REVIEW_PREVIEW_MISMATCH"
    assert result["source"] == "review_preview"
    assert result["preview_revision"] == original
    assert result["current_revision"] == tools.sha256_revision(path.read_bytes())
    assert "base_revision" not in result
    assert path.read_text(encoding="utf-8") == "a newer"
    assert counts == {"write": 1, "history": 1}  # inner handler only


@pytest.mark.parametrize("kind", ["edit", "write"])
async def test_stable_effective_review_allows_write(target, monkeypatch, kind):
    path, counts = target
    reviewing(monkeypatch, path, gate({"verdict": "ok"}), mutate=False)
    result = await invoke(kind, path)
    assert result["exit_code"] == 0
    assert path.read_text(encoding="utf-8") == "A old"
    assert counts == {"write": 1, "history": 1}


@pytest.mark.parametrize("kind", ["edit", "write"])
@pytest.mark.parametrize("review", [None, {"verdict": "ok", "error": "timeout"}, {"verdict": "ok", "unparsed": True}])
async def test_noop_and_failed_review_keep_fail_open(target, monkeypatch, kind, review):
    path, counts = target
    reviewing(monkeypatch, path, gate(review))
    result = await invoke(kind, path)
    assert result["exit_code"] == 0
    assert path.read_text(encoding="utf-8") == ("A newer" if kind == "edit" else "A old")
    assert counts == {"write": 2, "history": 2}


@pytest.mark.parametrize("kind", ["edit", "write"])
async def test_denied_review_returns_existing_result_without_write(target, monkeypatch, kind):
    path, counts = target
    denied = {"error": "synthetic denial", "exit_code": 1, "status": "doubt_review_blocked"}
    reviewing(monkeypatch, path, {"proceed": False, "result": denied}, mutate=False)
    assert await invoke(kind, path) == denied
    assert counts == {"write": 0, "history": 0}


async def test_creation_during_missing_file_review_is_conflict(target, monkeypatch):
    path, counts = target
    path.unlink()
    reviewing(monkeypatch, path, gate({"verdict": "ok"}))
    result = await invoke("write", path)
    assert result["error_code"] == "REVIEW_PREVIEW_MISMATCH"
    assert result["preview_revision"] is None
    assert result["current_revision"] == tools.sha256_revision(path.read_bytes())
    assert path.read_text(encoding="utf-8") == "a newer"
    assert counts == {"write": 1, "history": 1}


async def test_still_missing_file_can_be_created_after_review(target, monkeypatch):
    path, counts = target
    path.unlink()
    reviewing(monkeypatch, path, gate({"verdict": "ok"}), mutate=False)
    result = await invoke("write", path)
    assert result["exit_code"] == 0
    assert counts == {"write": 1, "history": 1}


@pytest.mark.parametrize("kind", ["edit", "write"])
async def test_reused_effective_review_dict_still_guards_local_revision(target, monkeypatch, kind):
    path, _ = target
    cached = gate({"verdict": "ok", "concerns": [], "model": "synthetic_cached"})
    reviewing(monkeypatch, path, cached)
    result = await invoke(kind, path)
    assert result["error_code"] == "REVIEW_PREVIEW_MISMATCH"


@pytest.mark.parametrize("error", [PermissionError("synthetic unreadable"), UnicodeDecodeError("utf8", b"x", 0, 1, "synthetic")])
async def test_unknown_preview_read_does_not_claim_missing(target, monkeypatch, error):
    path, counts = target
    real_read = tools._read_text_lf
    reads = []

    def read(p):
        reads.append(p)
        if len(reads) == 1:
            raise error
        return real_read(p)

    monkeypatch.setattr(tools, "_read_text_lf", read)
    reviewing(monkeypatch, path, gate({"verdict": "ok"}))
    result = await invoke("write", path)
    assert result["exit_code"] == 0
    assert "preview_revision" not in result
    assert counts == {"write": 2, "history": 2}


async def test_file_deleted_after_edit_review_returns_preview_conflict(target, monkeypatch):
    path, counts = target

    async def check(*a, **kw):
        path.unlink()
        return gate({"verdict": "ok"})

    monkeypatch.setattr(doubt_review, "check_edit", check)
    result = await invoke("edit", path)
    assert result["error_code"] == "REVIEW_PREVIEW_MISMATCH"
    assert result["current_revision"] is None
    assert counts == {"write": 0, "history": 0}
