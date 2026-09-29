"""Prepared patch guards and shared worker locks on temporary files."""
import asyncio
from contextlib import contextmanager
import json
import os
import threading

import pytest

from src import doubt_review, edit_journal, file_mutation_locks as locks, tool_execution
from src.agent_tools import filesystem_tools as tools


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(tool_execution, "_resolve_tool_path", lambda p: p)
    monkeypatch.setattr(doubt_review, "enabled", lambda: False)
    monkeypatch.setattr(tools, "record_edit_history", lambda *a, **k: None)
    calls = []
    real = edit_journal.apply_batch

    def batch(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(edit_journal, "apply_batch", batch)
    return calls


def patch(*ops):
    return "*** Begin Patch\n" + "\n".join(ops) + "\n*** End Patch"


def update(path):
    return f"*** Update File: {path}\n@@\n-old\n+NEW"


async def write(path, content):
    return await tools.WriteFileTool().execute(json.dumps({"path": str(path), "content": content}), {"inner": True})


@pytest.mark.parametrize("kind", ["update", "add", "delete"])
async def test_stale_prepared_target_rejects_entire_batch_before_journal(tmp_path, monkeypatch, isolated, kind):
    trigger, target = tmp_path / "trigger.txt", tmp_path / "target.txt"
    trigger.write_text("old\n", encoding="utf-8")
    if kind != "add":
        target.write_text("old\n", encoding="utf-8")
    op = update(target) if kind == "update" else (f"*** Add File: {target}\n+NEW" if kind == "add" else f"*** Delete File: {target}")
    monkeypatch.setattr(doubt_review, "enabled", lambda: True)
    monkeypatch.setattr(doubt_review, "block_mode_enabled", lambda: True)
    mutated = []

    async def review(ctx, *a, **kw):
        if not ctx.get("inner") and not mutated:
            mutated.append(True)
            assert (await write(target, "concurrent\n"))["exit_code"] == 0
        return {"proceed": True, "section": None, "review": None}

    monkeypatch.setattr(doubt_review, "check_edit", review)
    result = await tools.ApplyPatchTool().execute(patch(update(trigger), op), {})
    assert result["status"] == "conflict" and result["error_code"] == "PATCH_PREPARATION_MISMATCH"
    assert result["source"] == "patch_preparation"
    assert "base_revision" not in result
    assert isolated == []
    assert trigger.read_text(encoding="utf-8") == "old\n"
    assert target.read_text(encoding="utf-8") == "concurrent\n"


async def test_valid_update_add_delete_batch(tmp_path, isolated):
    existing, added, removed = [tmp_path / name for name in ["existing", "added", "removed"]]
    existing.write_text("old\n", encoding="utf-8")
    removed.write_text("old\n", encoding="utf-8")
    result = await tools.ApplyPatchTool().execute(patch(update(existing), f"*** Add File: {added}\n+ADDED", f"*** Delete File: {removed}"), {})
    assert result["exit_code"] == 0 and len(isolated) == 1
    assert existing.read_text(encoding="utf-8") == "NEW\n"
    assert added.read_text(encoding="utf-8") == "ADDED\n"
    assert not removed.exists()


@pytest.mark.parametrize("alias_kind", ["same", "relative", "case", "symlink"])
async def test_duplicate_canonical_targets_rejected_by_real_parser(tmp_path, monkeypatch, isolated, alias_kind):
    path = tmp_path / "Target.txt"
    path.write_text("old\n", encoding="utf-8")
    alias = str(path)
    if alias_kind == "relative":
        alias = str(tmp_path / ".." / tmp_path.name / path.name)
    elif alias_kind == "case":
        if os.name != "nt":
            pytest.skip("Windows case aliases")
        alias = alias.swapcase()
    elif alias_kind == "symlink":
        link = tmp_path / "link.txt"
        try:
            link.symlink_to(path)
        except OSError as exc:
            pytest.skip(str(exc))
        alias = str(link)
    result = await tools.ApplyPatchTool().execute(patch(update(path), update(alias)), {})
    assert result["error_code"] == "DUPLICATE_PATCH_TARGET"
    assert isolated == [] and path.read_text(encoding="utf-8") == "old\n"


async def test_inverse_order_batches_do_not_deadlock(tmp_path, monkeypatch, isolated):
    paths = [tmp_path / "a", tmp_path / "b"]
    for p in paths:
        p.write_text("old\n", encoding="utf-8")
    barrier = threading.Barrier(2, timeout=5)
    real = tools.mutation_locks

    @contextmanager
    def start_together(ps):
        barrier.wait()
        with real(ps):
            yield

    monkeypatch.setattr(tools, "mutation_locks", start_together)
    results = await asyncio.wait_for(asyncio.gather(*[
        tools.ApplyPatchTool().execute(patch(*(update(p) for p in order)), {})
        for order in [paths, list(reversed(paths))]
    ]), 10)
    assert sorted(r["exit_code"] for r in results) == [0, 1]
    assert len(isolated) == 1
    assert [p.read_text(encoding="utf-8") for p in paths] == ["NEW\n", "NEW\n"]


async def test_compensation_retains_mutex_against_other_handler(tmp_path, monkeypatch, isolated):
    first, second = tmp_path / "a", tmp_path / "b"
    for p in [first, second]:
        p.write_text("old\n", encoding="utf-8")
    compensating, waiter_attempted = threading.Event(), threading.Event()
    real_write, real_batch, real_lock = tools._write_text_lf, edit_journal.apply_batch, tools.mutation_lock

    def fail_second(p, *a):
        if p == str(second):
            raise OSError("synthetic second-op failure")
        return real_write(p, *a)

    def batch(*a, **kw):
        real_restore = kw["write_bytes"]

        def restore(p, data):
            compensating.set()
            assert waiter_attempted.wait(5)
            assert first.read_text(encoding="utf-8") == "NEW\n"
            return real_restore(p, data)

        kw["write_bytes"] = restore
        return real_batch(*a, **kw)

    @contextmanager
    def waiter_lock(p):
        waiter_attempted.set()
        with real_lock(p):
            yield

    monkeypatch.setattr(tools, "_write_text_lf", fail_second)
    monkeypatch.setattr(edit_journal, "apply_batch", batch)
    monkeypatch.setattr(tools, "mutation_lock", waiter_lock)
    task = asyncio.create_task(tools.ApplyPatchTool().execute(patch(update(first), update(second)), {}))
    assert await asyncio.to_thread(compensating.wait, 5)
    following = asyncio.create_task(write(first, "after rollback\n"))
    failed, succeeded = await asyncio.wait_for(asyncio.gather(task, following), 10)
    assert failed["status"] == "partial" and failed["journal"]["rolled_back"] is True
    assert succeeded["exit_code"] == 0
    assert first.read_text(encoding="utf-8") == "after rollback\n"
    assert second.read_text(encoding="utf-8") == "old\n"


async def test_validation_read_error_is_not_absence_and_releases_all_locks(tmp_path, monkeypatch, isolated):
    path = tmp_path / "a"
    other = tmp_path / "b"
    path.write_text("old\n", encoding="utf-8")
    other.write_text("old\n", encoding="utf-8")
    real_open = open

    def denied(p, mode="r", *a, **kw):
        if str(p) == str(path) and mode == "rb":
            raise PermissionError("synthetic validation read denied")
        return real_open(p, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", denied)
    result = await tools.ApplyPatchTool().execute(patch(update(other), update(path)), {})
    assert result["exit_code"] == 1 and "validation read denied" in result["error"]
    assert result.get("status") != "conflict" and isolated == []
    monkeypatch.setattr("builtins.open", real_open)
    assert (await asyncio.wait_for(write(path, "following\n"), 5))["exit_code"] == 0
    assert (await asyncio.wait_for(write(other, "following\n"), 5))["exit_code"] == 0


def test_nested_same_path_alias_lock_is_reentrant(tmp_path):
    path = tmp_path / "a"
    with locks.mutation_lock(path):
        with locks.mutation_locks([path, tmp_path / ".." / tmp_path.name / "a"]):
            pass


async def test_moves_remain_unsupported(tmp_path, isolated):
    path = tmp_path / "a"
    path.write_text("old\n", encoding="utf-8")
    text = patch(f"*** Update File: {path}\n*** Move to: {tmp_path / 'b'}\n@@\n-old\n+NEW")
    result = await tools.ApplyPatchTool().execute(text, {})
    assert result["exit_code"] == 1 and "move operations are not supported" in result["error"]
    assert isolated == []
