"""Whole-curation serialization over real, disposable SQLite writes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading

import pytest

from src import memory_curator as curator
from src import memory_engine as engine


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    yield
    engine.reset_vector_store()


@pytest.mark.parametrize("second_scope", [("alice", "atlas"), ("bob", "other")])
@pytest.mark.parametrize("pause_at", ["selection", "save_before_delete"])
def test_second_curator_cannot_select_until_first_finishes(store, monkeypatch, second_scope, pause_at):
    # Global rows are visible to both scopes: separate per-owner locks would
    # still permit the two curators to mutate the same rows.
    for ref in ("first", "second"):
        engine.add_item("Run verification before publishing", now=NOW,
            evidence=[{"kind": "document", "ref": ref}])
    first_selected, release_first = threading.Event(), threading.Event()
    second_attempted, second_selected = threading.Event(), threading.Event()
    lock = getattr(curator, "_CURATION_LOCK", threading.RLock())
    thread_role = threading.local()
    original_select = engine.scoped_items
    original_save = engine.save_item

    def pause():
        first_selected.set()
        assert release_first.wait(5), "Main thread did not release first curator"

    class ObservedLock:
        def __enter__(self):
            if thread_role.role == "second":
                second_attempted.set()
            lock.acquire()

        def __exit__(self, *args):
            lock.release()

    # Observe a real acquisition attempt rather than infer scheduling from a
    # sleep. On the legacy implementation this lock is unused; selection below
    # signals the attempt instead, and exposes the overlapping snapshot.
    monkeypatch.setattr(curator, "_CURATION_LOCK", ObservedLock(), raising=False)

    def select(*args, **kwargs):
        rows = original_select(*args, **kwargs)
        if thread_role.role == "first":
            if pause_at == "selection":
                pause()
        else:
            second_selected.set()
            second_attempted.set()
        return rows

    def save(item):
        result = original_save(item)
        if thread_role.role == "first" and pause_at == "save_before_delete":
            pause()
        return result

    monkeypatch.setattr(engine, "scoped_items", select)
    monkeypatch.setattr(engine, "save_item", save)

    def run(role, scope):
        thread_role.role = role
        return curator.curate(*scope, now=NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, "first", ("alice", "atlas"))
        second = None
        try:
            assert first_selected.wait(5)
            second = pool.submit(run, "second", second_scope)
            assert second_attempted.wait(5)
            assert not second_selected.is_set(), "Concurrent curator selected a stale snapshot"
        finally:
            release_first.set()
        reports = [first.result(timeout=5), second.result(timeout=5)]

    # Restore selection before reopening through the engine's fresh connection.
    monkeypatch.setattr(engine, "scoped_items", original_select)
    rows = engine.scoped_items()
    assert len(rows) == 1
    assert {event["ref"] for event in rows[0]["evidence"]} == {"first", "second"}
    assert [report["deduped"] for report in reports] == [1, 0]
    assert [report["total_active"] for report in reports] == [1, 1]


@pytest.mark.parametrize("operation", ["scoped_items", "save_item"])
def test_failed_curator_releases_lock_for_another_thread(store, monkeypatch, operation):
    for ref in ("first", "second"):
        engine.add_item("Run verification before publishing", now=NOW,
            evidence=[{"kind": "document", "ref": ref}])
    original = getattr(engine, operation)

    def broken(*args, **kwargs):
        if operation == "save_item":
            original(*args, **kwargs)
        raise RuntimeError("Synthetic curator failure")

    monkeypatch.setattr(engine, operation, broken)
    assert curator.safe_curate(now=NOW)["error"] == "Synthetic curator failure"
    monkeypatch.setattr(engine, operation, original)
    reports, errors = [], []

    def retry():
        try:
            reports.append(curator.curate(now=NOW))
        except Exception as exc:
            errors.append(exc)

    # A regression that leaks the lock must fail without hanging test shutdown.
    worker = threading.Thread(target=retry, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "Failed curator retained the process lock"
    assert errors == []
    report = reports[0]
    assert report["deduped"] == 1 and report["total_active"] == 1
    rows = engine.scoped_items()
    assert len(rows) == 1
    assert {event["ref"] for event in rows[0]["evidence"]} == {"first", "second"}
