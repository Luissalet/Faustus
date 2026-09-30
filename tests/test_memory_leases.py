"""Leases, fencing and watermarks for background memory work (H20)."""

import asyncio
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import pytest

from src import work_lease as wl


@pytest.fixture
def db(tmp_path):
    return tmp_path / "leases.sqlite3"


# --- the lease ------------------------------------------------------------------


def test_a_second_claim_is_refused_until_the_lease_expires(db):
    first = wl.acquire("job:a", "worker-1", ttl=60, now=1000, path=db)
    assert isinstance(first, wl.Lease) and first.fence == 1
    denied = wl.acquire("job:a", "worker-2", ttl=60, now=1030, path=db)
    assert isinstance(denied, wl.LeaseDenied) and denied.reason == "held"
    assert denied.held_by == "worker-1" and "worker-1" in denied.message
    taken = wl.acquire("job:a", "worker-2", ttl=60, now=1061, path=db)
    assert isinstance(taken, wl.Lease) and taken.fence == 2


def test_scopes_do_not_block_each_other(db):
    assert isinstance(wl.acquire("job:a", "w", path=db), wl.Lease)
    assert isinstance(wl.acquire("job:b", "w", path=db), wl.Lease)


def test_renew_extends_a_held_lease_and_fails_after_takeover(db):
    lease = wl.acquire("job:a", "w1", ttl=10, now=1000, path=db)
    assert wl.renew(lease, ttl=100, now=1005)
    assert isinstance(wl.acquire("job:a", "w2", ttl=10, now=1050, path=db), wl.LeaseDenied)
    wl.acquire("job:a", "w2", ttl=10, now=1200, path=db)  # the renewed lease ran out
    assert wl.renew(lease, ttl=100, now=1201) is False


def test_a_stale_worker_cannot_publish_or_record_progress(db):
    old = wl.acquire("job:a", "old", ttl=10, now=1000, path=db)
    new = wl.acquire("job:a", "new", ttl=10, now=2000, path=db)
    assert new.fence == old.fence + 1
    with wl.commit_window(old) as window:
        assert window.ok is False
    assert wl.release(old, success=True, input_revision="r", algorithm_version="v") is False
    assert wl.watermark("job:a", path=db) is None  # the stale worker recorded nothing
    with wl.commit_window(new) as window:
        assert window.ok is True
    assert wl.release(new, success=True, input_revision="r2", algorithm_version="v", now=2001)
    assert wl.watermark("job:a", path=db)["input_revision"] == "r2"


def test_the_fence_keeps_growing_after_a_clean_release(db):
    a = wl.acquire("job:a", "w", path=db)
    wl.release(a, success=True, input_revision="r", algorithm_version="v")
    b = wl.acquire("job:a", "w", path=db)
    assert b.fence == a.fence + 1


def test_a_successor_cannot_be_granted_while_the_old_holder_is_publishing(db):
    old = wl.acquire("job:a", "old", ttl=1, now=time.time() - 100, path=db)  # already expired
    inside, finished_writing, successor_done = threading.Event(), threading.Event(), threading.Event()
    order = []

    def successor():
        inside.wait(5)
        lease = wl.acquire("job:a", "new", ttl=60, path=db)
        order.append("successor granted")
        successor_done.set()
        assert isinstance(lease, wl.Lease)

    thread = threading.Thread(target=successor)
    thread.start()
    with wl.commit_window(old) as window:
        assert window.ok  # nobody has taken it yet
        inside.set()
        time.sleep(0.4)   # the successor is waiting on the lock, not granted
        assert not successor_done.is_set()
        order.append("old published")
    thread.join(5)
    assert order == ["old published", "successor granted"]


def test_failures_back_off_exponentially_and_success_clears_it(db):
    lease = wl.acquire("job:a", "w", now=1000, path=db)
    wl.release(lease, success=False, now=1000)
    denied = wl.acquire("job:a", "w", now=1010, path=db)
    assert isinstance(denied, wl.LeaseDenied) and denied.reason == "backoff"
    assert denied.until == pytest.approx(1000 + wl.BACKOFF_BASE_SECONDS)
    second = wl.acquire("job:a", "w", now=1000 + wl.BACKOFF_BASE_SECONDS + 1, path=db)
    wl.release(second, success=False, now=1100)
    again = wl.acquire("job:a", "w", now=1101, path=db)
    assert again.until == pytest.approx(1100 + 2 * wl.BACKOFF_BASE_SECONDS)
    third = wl.acquire("job:a", "w", now=1100 + 2 * wl.BACKOFF_BASE_SECONDS + 1, path=db)
    wl.release(third, success=True, input_revision="r", algorithm_version="v", now=1300)
    assert isinstance(wl.acquire("job:a", "w", now=1301, path=db), wl.Lease)


def test_cheap_work_can_fail_without_backoff(db):
    lease = wl.acquire("job:a", "w", now=1000, path=db)
    wl.release(lease, success=False, now=1000, backoff=False)
    assert isinstance(wl.acquire("job:a", "w", now=1001, path=db), wl.Lease)


def test_the_watermark_needs_success_and_the_same_algorithm(db):
    lease = wl.acquire("job:a", "w", path=db)
    wl.release(lease, success=False, backoff=False)
    assert wl.is_unchanged("job:a", "r1", "v1", path=db) is False
    lease = wl.acquire("job:a", "w", path=db)
    wl.release(lease, success=True, input_revision="r1", algorithm_version="v1")
    assert wl.is_unchanged("job:a", "r1", "v1", path=db) is True
    assert wl.is_unchanged("job:a", "r2", "v1", path=db) is False
    assert wl.is_unchanged("job:a", "r1", "v2", path=db) is False


def test_a_lease_taken_in_another_process_is_seen_here(db):
    code = (
        "import sys; from pathlib import Path; from src import work_lease as wl; "
        "l = wl.acquire('job:x', 'other-process', ttl=600, path=Path(sys.argv[1])); "
        "print(type(l).__name__)"
    )
    out = subprocess.run([sys.executable, "-c", code, str(db)], capture_output=True, text=True,
                         cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    assert out.stdout.strip() == "Lease", out.stderr
    denied = wl.acquire("job:x", "this-process", path=db)
    assert isinstance(denied, wl.LeaseDenied) and denied.held_by == "other-process"


def test_status_lists_holder_expiry_backoff_and_watermark(db):
    a = wl.acquire("job:a", "w1", ttl=10, now=1000, path=db)
    b = wl.acquire("job:b", "w2", ttl=10, now=1000, path=db)
    wl.release(b, success=True, input_revision="r", algorithm_version="v", now=1001)
    rows = {r["scope"]: r for r in wl.status(now=1500, path=db)}
    assert rows["job:a"]["held_by"] == "w1" and rows["job:a"]["lease_expired"] is True
    assert rows["job:b"]["held_by"] is None and rows["job:b"]["watermark"]["input_revision"] == "r"
    assert wl.status("job:a", now=1500, path=db)[0]["scope"] == "job:a"
    assert a.fence == 1


# --- the legacy memory tidy-up ---------------------------------------------------------


def _action():
    from src.builtin_actions import action_consolidate_memory
    return action_consolidate_memory


def _memories(data_dir):
    return json.loads((data_dir / "memory.json").read_text(encoding="utf-8"))


@pytest.fixture
def tidy(tmp_path, monkeypatch):
    """A data folder with memories for one owner and a scripted model."""
    from src import constants, llm_core, task_endpoint

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rows = [
        {"id": "m1", "owner": "ada", "text": "Ada prefers short answers.", "category": "preference"},
        {"id": "m2", "owner": "ada", "text": "Ada's project uses Postgres for storage.", "category": "project"},
        {"id": "m3", "owner": "ada", "text": "Old note that is no longer relevant.", "category": "fact"},
    ]
    (data_dir / "memory.json").write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(constants, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(task_endpoint, "resolve_task_candidates",
                        lambda *a, **k: [("http://llm", "model", {})])

    class Model:
        calls = 0
        during = None  # optional async hook run while the model is "thinking"
        answer = {"keep": [], "drop": [{"id": "m3", "reason": "obsolete"}]}

    async def fake(_candidates, **kwargs):
        Model.calls += 1
        if Model.during is not None:
            await Model.during()
        return json.dumps(Model.answer)

    monkeypatch.setattr(llm_core, "llm_call_async_with_fallback", fake)
    Model.data_dir = data_dir
    return Model


def _add_memory(data_dir, entry):
    path = data_dir / "memory.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows.append(entry)
    path.write_text(json.dumps(rows), encoding="utf-8")


@pytest.mark.asyncio
async def test_a_tidy_with_nothing_new_since_the_last_one_does_not_call_the_model(tidy):
    from src.builtin_actions import TaskNoop
    message, ok = await _action()("ada")
    assert ok and tidy.calls == 1 and [m["id"] for m in _memories(tidy.data_dir)] == ["m1", "m2"]
    with pytest.raises(TaskNoop, match="no new memory work"):
        await _action()("ada")
    assert tidy.calls == 1
    _add_memory(tidy.data_dir, {"id": "m4", "owner": "ada", "text": "Ada moved to Lisbon.", "category": "fact"})
    tidy.answer = {"keep": [], "drop": []}
    with pytest.raises(TaskNoop, match="no duplicates"):
        await _action()("ada")  # a new memory is new work: the model was asked again
    assert tidy.calls == 2


@pytest.mark.asyncio
async def test_the_use_counter_is_not_new_work(tidy):
    await _action()("ada")
    path = tidy.data_dir / "memory.json"
    rows = _memories(tidy.data_dir)
    rows[0]["uses"] = 40
    rows[0]["timestamp"] = 1
    path.write_text(json.dumps(rows), encoding="utf-8")
    from src.builtin_actions import TaskNoop
    with pytest.raises(TaskNoop):
        await _action()("ada")


@pytest.mark.asyncio
async def test_two_tidies_at_once_run_the_model_once(tidy):
    from src.builtin_actions import TaskNoop
    started, release = asyncio.Event(), asyncio.Event()

    async def hold():
        started.set()
        await release.wait()

    tidy.during = hold
    first = asyncio.ensure_future(_action()("ada"))
    await started.wait()
    with pytest.raises(TaskNoop, match="already being worked on"):
        await _action()("ada")
    release.set()
    message, ok = await first
    assert ok and tidy.calls == 1
    assert [m["id"] for m in _memories(tidy.data_dir)] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_other_owners_are_not_blocked_by_a_running_tidy(tidy):
    _add_memory(tidy.data_dir, {"id": "b1", "owner": "bob", "text": "Bob likes tea.", "category": "fact"})
    _add_memory(tidy.data_dir, {"id": "b2", "owner": "bob", "text": "Bob works nights.", "category": "fact"})
    started, release = asyncio.Event(), asyncio.Event()

    async def hold():
        started.set()
        await release.wait()

    tidy.during = hold
    first = asyncio.ensure_future(_action()("ada"))
    await started.wait()
    tidy.during = None
    tidy.answer = {"keep": [], "drop": []}
    from src.builtin_actions import TaskNoop
    with pytest.raises(TaskNoop, match="no duplicates"):  # it ran: a different scope
        await _action()("bob")
    release.set()
    with pytest.raises(TaskNoop, match="no duplicates"):  # ada's own run finished too
        await first


@pytest.mark.asyncio
async def test_a_memory_saved_while_the_model_was_thinking_survives(tidy):
    async def during():
        _add_memory(tidy.data_dir, {"id": "late", "owner": "ada", "text": "Saved mid tidy.", "category": "fact"})

    tidy.during = during
    message, ok = await _action()("ada")
    ids = [m["id"] for m in _memories(tidy.data_dir)]
    assert ok and "late" in ids and "m3" not in ids


@pytest.mark.asyncio
async def test_an_entry_edited_meanwhile_is_not_dropped_on_a_stale_decision(tidy):
    async def during():
        path = tidy.data_dir / "memory.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        for row in rows:
            if row["id"] == "m3":
                row["text"] = "Old note, but the user rewrote it and it matters now."
        path.write_text(json.dumps(rows), encoding="utf-8")

    tidy.during = during
    message, ok = await _action()("ada")
    rows = {m["id"]: m for m in _memories(tidy.data_dir)}
    assert "m3" in rows and "rewrote it" in rows["m3"]["text"]
    assert ok  # the decision was applied where it still held, nothing else


@pytest.mark.asyncio
async def test_a_tidy_whose_lease_was_taken_over_publishes_nothing(tidy):
    from src import memory_consolidation as mc
    before = (tidy.data_dir / "memory.json").read_text(encoding="utf-8")

    async def during():
        # a successor is granted the scope while this worker is still deciding
        assert isinstance(wl.acquire(mc.scope_for("ada"), "successor", now=time.time() + 10_000),
                          wl.Lease)

    tidy.during = during
    message, ok = await _action()("ada")
    assert ok is False and "lease lost" in message
    assert (tidy.data_dir / "memory.json").read_text(encoding="utf-8") == before
    assert wl.watermark(mc.scope_for("ada")) is None


@pytest.mark.asyncio
async def test_a_failed_publish_backs_off_and_the_next_try_is_told_why(tidy, monkeypatch):
    from src.builtin_actions import TaskNoop
    from src.memory import MemoryManager

    def boom(self, entries):
        raise OSError("disk full")

    monkeypatch.setattr(MemoryManager, "save", boom)
    message, ok = await _action()("ada")
    assert ok is False and "disk full" in message
    from src import memory_consolidation as mc
    row = wl.status(mc.scope_for("ada"))[0]
    assert row["failures"] == 1 and row["backing_off"] is True and row["held_by"] is None
    with pytest.raises(TaskNoop, match="backing off"):
        await _action()("ada")
    assert tidy.calls == 1


@pytest.mark.asyncio
async def test_an_unusable_lease_store_does_not_stop_memory_upkeep(tidy, monkeypatch):
    def broken(*a, **k):
        raise wl.sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(wl, "acquire", broken)
    message, ok = await _action()("ada")
    assert ok and "m3" not in [m["id"] for m in _memories(tidy.data_dir)]


# --- the learned-memory curator -----------------------------------------------------------


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


@pytest.fixture
def curator_store(tmp_path, monkeypatch):
    from src import memory_engine as engine
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(wl, "default_path", lambda: tmp_path / "leases.sqlite3")
    engine.set_vector_store(None)
    for ref in ("first", "second"):
        engine.add_item("Run verification before publishing", now=NOW,
                        evidence=[{"kind": "document", "ref": ref}])
    yield engine
    engine.reset_vector_store()


def test_a_scope_being_curated_elsewhere_is_skipped_not_duplicated(curator_store):
    from src import memory_curator as curator
    held = wl.acquire(curator._scope_name("ada", None), "another-process")
    report = curator.curate("ada", None, now=NOW)
    assert report["skipped"] == "held" and report["deduped"] == 0
    assert len(curator_store.scoped_items("ada", None, curator_store.STATUSES)) == 2
    wl.release(held, success=True, input_revision="", algorithm_version="x")
    assert curator.curate("ada", None, now=NOW)["deduped"] == 1


def test_a_curation_that_lost_its_lease_stops_before_its_next_pass(curator_store, monkeypatch):
    from src import memory_curator as curator
    calls = []
    real_dedupe = curator._dedupe

    def dedupe_then_lose_the_lease(items, now, report):
        out = real_dedupe(items, now, report)
        wl.acquire(curator._scope_name("ada", None), "successor", now=time.time() + 10_000)
        return out

    monkeypatch.setattr(curator, "_dedupe", dedupe_then_lose_the_lease)
    monkeypatch.setattr(curator, "_invert", lambda *a, **k: calls.append("invert"))
    report = curator.curate("ada", None, now=NOW)
    assert report["skipped"] == "lease_lost" and calls == []


def test_an_unchanged_scope_is_skipped_only_when_asked_and_only_within_the_day(curator_store):
    from src import memory_curator as curator
    first = curator.curate("ada", None, now=NOW, skip_if_unchanged=True)
    assert "skipped" not in first
    again = curator.curate("ada", None, now=NOW, skip_if_unchanged=True)
    assert again["skipped"] == "no_new_work"
    assert "skipped" not in curator.curate("ada", None, now=NOW)  # an explicit run always runs
    tomorrow = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    assert "skipped" not in curator.curate("ada", None, now=tomorrow, skip_if_unchanged=True)
    curator_store.add_item("A brand new rule about commits", now=NOW,
                           evidence=[{"kind": "document", "ref": "x"}])
    assert "skipped" not in curator.curate("ada", None, now=tomorrow, skip_if_unchanged=True)
    assert curator.curate("ada", None, now=tomorrow, skip_if_unchanged=True)["skipped"] == "no_new_work"
    curator_store.add_item("Another rule about review", now=NOW,
                           evidence=[{"kind": "document", "ref": "y"}])
    assert "skipped" not in curator.curate("ada", None, now=tomorrow, skip_if_unchanged=True)


def test_a_failed_curation_releases_its_lease_without_backoff(curator_store, monkeypatch):
    from src import memory_curator as curator

    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    real = curator._dedupe
    monkeypatch.setattr(curator, "_dedupe", boom)
    with pytest.raises(RuntimeError):
        curator.curate("ada", None, now=NOW)
    monkeypatch.setattr(curator, "_dedupe", real)
    assert curator.curate("ada", None, now=NOW)["deduped"] == 1  # not held, not backing off


def test_an_unusable_lease_store_does_not_stop_curation(curator_store, monkeypatch):
    from src import memory_curator as curator

    def broken(*a, **k):
        raise wl.sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(wl, "acquire", broken)
    assert curator.curate("ada", None, now=NOW)["deduped"] == 1


# --- read surfaces -----------------------------------------------------------------------------


def test_the_jobs_route_and_the_mcp_tool_report_holder_backoff_and_watermark(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.memory_engine_routes import setup_memory_engine_routes
    monkeypatch.setattr(wl, "default_path", lambda: tmp_path / "leases.sqlite3")
    done = wl.acquire("memory-tidy:ada", "w1")
    wl.release(done, success=True, input_revision="r", algorithm_version="v")
    failed = wl.acquire("memory-tidy:bob", "w2")
    wl.release(failed, success=False)
    wl.acquire("memory-curate:*:*", "curator:1")
    wl.acquire("something-else", "x")  # not a memory job

    app = FastAPI()
    app.include_router(setup_memory_engine_routes())
    body = TestClient(app).get("/api/memory-engine/jobs").json()
    scopes = {r["scope"]: r for r in body["jobs"]}
    assert set(scopes) == {"memory-tidy:ada", "memory-tidy:bob", "memory-curate:*:*"}
    assert scopes["memory-tidy:bob"]["backing_off"] and scopes["memory-curate:*:*"]["held_by"] == "curator:1"

    from tests.test_dispatch import _load_workers_server
    ws = _load_workers_server(monkeypatch)
    assert "memory_jobs" in {t.name for t in ws.TOOLS}
    monkeypatch.setattr(ws, "_request", lambda method, path, body=None, **kw: {"jobs": list(scopes.values())})
    text = asyncio.run(ws.call_tool("memory_jobs", {}))[0].text
    assert "memory-tidy:bob: backing off after 1 failure(s)" in text
    assert "memory-curate:*:*: held by curator:1" in text
    assert "memory-tidy:ada: idle" in text and "last finished at" in text
