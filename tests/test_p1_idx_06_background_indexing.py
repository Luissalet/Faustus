"""IDX-06 — grandes colecciones sin secuestrar el PC.

`src/code_index.py::refresh` gains three things this lote, all reusing
existing authorities rather than inventing new ones (rule 4):

1. **pause under resource pressure** — reuses
   `src.bg_monitor.is_paused_for_resource_pressure()`, the SAME guard PERF-04
   already wired into `src/bg_jobs.py`, via an injectable monkeypatch so the
   test never touches real RAM.
2. **dedup by content** — a file whose hash already belongs to another
   indexed path is recorded from that twin instead of reparsed.
3. **orphan cleanup** — `cleanup_orphaned_workspaces()` drops rows for a
   workspace directory that no longer exists, without touching any that do.

Plus `refresh_async`, the off-the-event-loop wrapper a route should await
instead of calling the blocking function directly.
"""

import asyncio
import os

import pytest

from src import code_index as ci
from src import bg_monitor
from src.context_engine import store


@pytest.fixture()
def ce_store(tmp_path):
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        store.use_path(None)


def write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


SAMPLE = '''def helper(x):
    return x + 1


def other(y):
    return helper(y) * 2
'''


@pytest.fixture()
def workspace(tmp_path):
    root = str(tmp_path / "repo")
    os.makedirs(root, exist_ok=True)
    write(root, "a.py", SAMPLE)
    return root


# ── pause under resource pressure ───────────────────────────────────────────

def test_refresh_pauses_under_pressure_and_does_not_scan(ce_store, workspace, monkeypatch):
    monkeypatch.setattr(bg_monitor, "is_paused_for_resource_pressure", lambda: True)
    result = ci.refresh(workspace, project_id="p1")
    assert result["paused"] is True
    assert result["scanned"] == 0
    assert result["reindexed"] == 0
    # Nothing was indexed — a later call, once pressure clears, is a normal
    # incremental pass, not a resume-from-nowhere.
    assert ci.status(workspace, project_id="p1")["files"] == 0


def test_refresh_proceeds_normally_when_not_under_pressure(ce_store, workspace, monkeypatch):
    monkeypatch.setattr(bg_monitor, "is_paused_for_resource_pressure", lambda: False)
    result = ci.refresh(workspace, project_id="p1")
    assert result["paused"] is False
    assert result["reindexed"] == 1


def test_pause_on_pressure_false_ignores_the_guard(ce_store, workspace, monkeypatch):
    monkeypatch.setattr(bg_monitor, "is_paused_for_resource_pressure", lambda: True)
    result = ci.refresh(workspace, project_id="p1", pause_on_pressure=False)
    assert result["paused"] is False
    assert result["reindexed"] == 1


def test_pressure_trip_mid_scan_stops_without_losing_earlier_writes(ce_store, tmp_path, monkeypatch):
    root = str(tmp_path / "repo2")
    os.makedirs(root, exist_ok=True)
    for i in range(ci.PRESSURE_CHECK_EVERY * 2 + 3):
        write(root, f"m{i}.py", f"def f{i}():\n    return {i}\n")

    calls = {"n": 0}

    def flaky_pressure():
        calls["n"] += 1
        # Clear on the very first (pre-scan) check, trip on the first
        # mid-scan check — proves files processed before the trip are kept.
        return calls["n"] > 1

    monkeypatch.setattr(bg_monitor, "is_paused_for_resource_pressure", flaky_pressure)
    result = ci.refresh(root, project_id="p1", budget_files=10_000)
    assert result["paused"] is True
    assert result["truncated"] is True
    assert 0 < result["reindexed"] < ci.PRESSURE_CHECK_EVERY * 2 + 3
    # The files processed before the trip are actually committed.
    assert ci.status(root, project_id="p1")["files"] == result["reindexed"]


# ── dedup by content ─────────────────────────────────────────────────────────

def test_identical_content_at_two_paths_is_deduped(ce_store, tmp_path, monkeypatch):
    monkeypatch.setattr(bg_monitor, "is_paused_for_resource_pressure", lambda: False)
    root = str(tmp_path / "repo3")
    os.makedirs(root, exist_ok=True)
    write(root, "original/util.py", SAMPLE)
    write(root, "thirdparty/copy_of_util.py", SAMPLE)  # byte-identical duplicate

    result = ci.refresh(root, project_id="p1")
    assert result["deduped"] == 1
    assert result["reindexed"] == 2  # both files ARE recorded...

    # ...and the copy's symbols came from the twin, not a second parse:
    defs = ci.find_definition("helper", workspace=root, project_id="p1")
    paths = {d["path"] for d in defs}
    assert paths == {"original/util.py", "thirdparty/copy_of_util.py"}


# ── orphan cleanup ───────────────────────────────────────────────────────────

def test_cleanup_orphaned_workspaces_drops_a_deleted_workspace_only(ce_store, tmp_path, monkeypatch):
    monkeypatch.setattr(bg_monitor, "is_paused_for_resource_pressure", lambda: False)
    gone = str(tmp_path / "will-be-deleted")
    os.makedirs(gone, exist_ok=True)
    write(gone, "a.py", SAMPLE)
    ci.refresh(gone, project_id="p1")

    stays = str(tmp_path / "stays")
    os.makedirs(stays, exist_ok=True)
    write(stays, "b.py", SAMPLE)
    ci.refresh(stays, project_id="p1")

    assert ci.status(gone, project_id="p1")["files"] == 1
    import shutil
    shutil.rmtree(gone)

    result = ci.cleanup_orphaned_workspaces()
    assert result["workspaces_removed"] == 1
    assert result["files_removed"] == 1
    assert ci.status(gone, project_id="p1")["files"] == 0
    assert ci.status(stays, project_id="p1")["files"] == 1  # untouched


# ── refresh_async ────────────────────────────────────────────────────────────

def test_refresh_async_matches_refresh(ce_store, workspace, monkeypatch):
    monkeypatch.setattr(bg_monitor, "is_paused_for_resource_pressure", lambda: False)
    result = asyncio.run(ci.refresh_async(workspace, project_id="p1"))
    assert result["reindexed"] == 1
    assert ci.status(workspace, project_id="p1")["files"] == 1


def test_refresh_async_does_not_block_the_event_loop(ce_store, tmp_path, monkeypatch):
    """The whole point of IDX-06's acceptance text: indexing a large folder
    must not block writing in the chat. Proven here as: another coroutine
    keeps making progress WHILE a (artificially slowed) refresh runs."""
    root = str(tmp_path / "repo4")
    os.makedirs(root, exist_ok=True)
    for i in range(50):
        write(root, f"m{i}.py", f"def f{i}():\n    return {i}\n")
    monkeypatch.setattr(bg_monitor, "is_paused_for_resource_pressure", lambda: False)

    real_extract = ci._extract

    def slow_extract(text, lang):
        import time as _time
        _time.sleep(0.005)
        return real_extract(text, lang)

    monkeypatch.setattr(ci, "_extract", slow_extract)

    async def scenario():
        ticks = []

        async def ticker():
            for _ in range(20):
                await asyncio.sleep(0.005)
                ticks.append(1)

        results = await asyncio.gather(
            ci.refresh_async(root, project_id="p1", budget_files=10_000),
            ticker(),
        )
        return results[0], ticks

    refresh_result, ticks = asyncio.run(scenario())
    assert refresh_result["reindexed"] == 50
    assert len(ticks) >= 5  # the event loop kept running other work meanwhile
