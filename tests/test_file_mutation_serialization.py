"""Deterministic worker interleavings on temporary files, without services."""
import asyncio
from contextlib import contextmanager
import gc
import json
import os
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from src import doubt_review, file_mutation_locks as locks, tool_execution
from src.agent_tools import filesystem_tools as tools


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(doubt_review, "enabled", lambda: False)
    monkeypatch.setattr(tools, "record_edit_history", lambda *a, **k: None)
    monkeypatch.setattr(tool_execution, "_resolve_tool_path", lambda p: p)


async def invoke(kind, path, base, label):
    args = {"path": str(path), "base_revision": base}
    if kind == "edit":
        args.update(old_string="a" if label == "first" else "b",
                    new_string="A" if label == "first" else "B")
        return await tools.EditFileTool().execute(json.dumps(args), {})
    args["content"] = "FIRST" if label == "first" else "SECOND"
    return await tools.WriteFileTool().execute(json.dumps(args), {})


@pytest.mark.parametrize("first,second", [("edit", "edit"), ("edit", "write"), ("write", "edit"), ("write", "write")])
async def test_same_revision_serializes_before_second_read(tmp_path, monkeypatch, isolated, first, second):
    path = tmp_path / "target.txt"
    path.write_text("a b", encoding="utf-8")
    base = tools.sha256_revision(path.read_bytes())
    first_read, second_attempt = threading.Event(), threading.Event()
    counter_guard = threading.Lock()
    attempts, reads = [], []
    real_lock, real_read = locks.mutation_lock, tools._read_text_lf

    @contextmanager
    def observed_lock(p):
        with counter_guard:
            attempts.append(p)
            if len(attempts) == 2:
                second_attempt.set()
        with real_lock(p):
            yield

    def paused_read(p):
        snapshot = real_read(p)
        with counter_guard:
            reads.append(snapshot[2])
            index = len(reads)
        if index == 1:
            first_read.set()
            assert second_attempt.wait(5), "second worker never attempted acquisition"
            assert len(reads) == 1, "second read entered the first critical section"
        return snapshot

    monkeypatch.setattr(tools, "mutation_lock", observed_lock)
    monkeypatch.setattr(tools, "_read_text_lf", paused_read)
    first_task = asyncio.create_task(invoke(first, path, base, "first"))
    assert await asyncio.to_thread(first_read.wait, 5)
    second_task = asyncio.create_task(invoke(second, path, base, "second"))
    a, b = await asyncio.wait_for(asyncio.gather(first_task, second_task), 10)
    assert a["exit_code"] == 0
    assert b["exit_code"] == 1 and b["status"] == "conflict"
    assert b["error_code"] == "BASE_REVISION_MISMATCH"
    assert reads[0] == base and reads[1] != base
    assert path.read_text(encoding="utf-8") == ("A b" if first == "edit" else "FIRST")


async def test_different_files_enter_workers_together(tmp_path, monkeypatch, isolated):
    paths = [tmp_path / "a.txt", tmp_path / "b.txt"]
    for p in paths:
        p.write_text("a b", encoding="utf-8")
    barrier = threading.Barrier(2, timeout=5)
    real_read = tools._read_text_lf

    def read(p):
        snapshot = real_read(p)
        barrier.wait()
        return snapshot

    monkeypatch.setattr(tools, "_read_text_lf", read)
    results = await asyncio.wait_for(asyncio.gather(*[
        invoke("edit", p, tools.sha256_revision(p.read_bytes()), "first") for p in paths
    ]), 10)
    assert [r["exit_code"] for r in results] == [0, 0]
    assert [p.read_text(encoding="utf-8") for p in paths] == ["A b", "A b"]


def assert_alias_excluded(original, alias):
    attempted, acquired = threading.Event(), threading.Event()

    def worker():
        attempted.set()
        with locks.mutation_lock(alias):
            acquired.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with locks.mutation_lock(original):
            task = pool.submit(worker)
            assert attempted.wait(5)
            assert not acquired.wait(0.05)
        task.result(timeout=5)
    assert acquired.is_set()


@pytest.mark.skipif(os.name != "nt", reason="Windows case normalization")
def test_windows_case_alias_uses_same_mutex(tmp_path):
    path = tmp_path / "MixedCase.txt"
    path.write_text("synthetic", encoding="utf-8")
    assert_alias_excluded(str(path), str(path).swapcase())


def test_symlink_alias_uses_same_mutex(tmp_path):
    path, alias = tmp_path / "target.txt", tmp_path / "alias.txt"
    path.write_text("synthetic", encoding="utf-8")
    try:
        alias.symlink_to(path)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    assert_alias_excluded(path, alias)


def test_exception_releases_mutex_for_other_thread(tmp_path):
    path = tmp_path / "target.txt"
    with pytest.raises(OSError):
        with locks.mutation_lock(path):
            raise OSError("synthetic write failure")
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(lambda: take_lock(path))
        assert task.result(timeout=5) is True


@pytest.mark.parametrize("kind", ["edit", "write"])
async def test_failed_handler_write_releases_for_following_call(tmp_path, monkeypatch, isolated, kind):
    path = tmp_path / "target.txt"
    path.write_text("a b", encoding="utf-8")
    base = tools.sha256_revision(path.read_bytes())
    real_write = tools._write_text_lf

    def fail(*args, **kwargs):
        raise OSError("synthetic failure before writing")

    monkeypatch.setattr(tools, "_write_text_lf", fail)
    failed = await invoke(kind, path, base, "first")
    assert failed["exit_code"] == 1 and "synthetic failure" in failed["error"]
    assert path.read_text(encoding="utf-8") == "a b"
    monkeypatch.setattr(tools, "_write_text_lf", real_write)
    result = await asyncio.wait_for(invoke("edit", path, base, "second"), 5)
    assert result["exit_code"] == 0
    assert path.read_text(encoding="utf-8") == "a B"


def take_lock(path):
    with locks.mutation_lock(path):
        return True


def test_unused_paths_do_not_accumulate_in_registry(tmp_path):
    for index in range(100):
        with locks.mutation_lock(tmp_path / str(index)):
            pass
    gc.collect()
    assert not any(key.startswith(locks.canonical_path(tmp_path)) for key in locks._LOCKS)
