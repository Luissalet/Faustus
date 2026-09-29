"""Per-run retention, latest-only readers and deletion use isolated files."""
import json
import os
import time
from pathlib import Path

import pytest

from src import agent_runs as runs


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runs, "_RUNS", {})
    monkeypatch.setattr(runs, "_INTERRUPTED", {})
    monkeypatch.setattr(runs, "_setting", lambda key, default: default)
    monkeypatch.setattr(runs, "_receipt_for_call_id", lambda key: None)
    monkeypatch.setattr(runs, "_artifacts_for_call_id", lambda key: [])


def make_run(sid="session", text="first", timestamp=None):
    run = runs._Run()
    if timestamp is not None:
        run.started_at = timestamp
    run.log = runs._RunLog(sid, run)
    runs._RUNS[sid] = run
    runs._publish(run, 'data: ' + json.dumps({"type": "tool_output", "tool": "test", "call_id": "call_1_0", "output": text}) + '\n\n')
    return run


def close_as_crash(run):
    run.log._f.flush()
    run.log.orphan()


class Sessions:
    def __init__(self):
        self.history = []
        self.saved = 0
    def get_session(self, sid):
        return self if sid == "session" else None
    def add_message(self, message):
        self.history.append(message)
    def save_sessions(self):
        self.saved += 1


def test_runs_are_exclusive_and_latest_trace_does_not_mix_reused_call_id():
    first = make_run(timestamp=1)
    first.log.finish("done")
    before = Path(first.log.path).read_bytes()
    second = make_run(text="second", timestamp=2)
    second.log.finish("done")
    assert first.log.path != second.log.path
    assert Path(first.log.path).read_bytes() == before
    runs._RUNS.clear()
    assert runs._log_path("session") == second.log.path
    trace = runs.trace_for_call("call_1_0", session_id="session")
    assert [event["output"] for event in trace["events"]] == ["second"]
    assert runs._trace_log_paths(None) == [second.log.path]
    collision = runs._RunLog("session", second)
    assert collision._f is None
    assert runs._read_log(second.log.path)["run_id"] == second.run_id


def test_legacy_reader_and_latest_per_run_remain_compatible():
    legacy = Path(runs._legacy_log_path("session"))
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"status": "running", "session_id": "session", "run_id": "old", "ts": 1}) + '\n')
    assert runs._log_path("session") == str(legacy)
    newer = make_run(timestamp=2)
    newer.log.finish("done")
    assert runs._log_path("session") == newer.log.path
    assert set(runs._session_log_paths("session")) == {str(legacy), newer.log.path}
    assert runs.recover_interrupted_runs(Sessions())[0]["run_id"] == "old"


def test_two_interrupted_runs_preserve_all_effect_warnings_and_do_not_duplicate():
    old = make_run(timestamp=1)
    runs.record_tool_effect("session", call_id="same", tool="send_email", effect_class="write", state="pending", durable=True)
    close_as_crash(old)
    new = make_run(timestamp=2)
    runs.record_tool_effect("session", call_id="same", tool="send_email", effect_class="write", state="unknown")
    close_as_crash(new)
    runs._RUNS.clear()
    sessions = Sessions()
    recovered = runs.recover_interrupted_runs(sessions)
    assert [row["run_id"] for row in recovered] == [old.run_id, new.run_id]
    assert len(sessions.history) == 2
    assert len(sessions.history[-1].metadata["unknown_effects"]) == 2
    assert {effect["run_id"] for effect in sessions.history[-1].metadata["unknown_effects"]} == {old.run_id, new.run_id}
    assert len(runs.interrupted_runs()[0]["runs"]) == 2
    block = runs.unknown_effects_system_block(sessions)
    assert old.run_id in block and new.run_id in block
    assert runs.recover_interrupted_runs(sessions) == []
    assert len(sessions.history) == 2


def test_crash_after_message_save_before_log_terminal_does_not_duplicate():
    run = make_run()
    close_as_crash(run)
    sessions = Sessions()
    from core.models import ChatMessage
    sessions.history.append(ChatMessage("assistant", "already saved", metadata={"run_id": run.run_id}))
    runs.recover_interrupted_runs(sessions)
    assert len(sessions.history) == 1 and sessions.saved == 1


def test_retention_removes_old_terminal_and_sidecar_but_not_recent_or_running():
    old = make_run("old")
    old.log.finish("done")
    recent = make_run("recent")
    recent.log.finish("done")
    running = make_run("running")
    close_as_crash(running)
    then = time.time() - 49 * 3600
    for path in (old.log.path, running.log.path):
        os.utime(path, (then, then))
    runs.recover_interrupted_runs()
    assert not Path(old.log.path).exists() and not Path(old.log.path + ".status").exists()
    assert Path(recent.log.path).exists() and Path(running.log.path).exists()


def test_purge_closes_old_and_current_writers_without_crossing_session_scope():
    first = make_run("a/b")
    second = make_run("a/b")
    other = make_run("a_b")
    assert runs.purge_session_logs("a/b") == 2
    for run in (first, second):
        run.log.finish("done")
        assert not Path(run.log.path).exists()
        assert not Path(run.log.path + ".status").exists()
    assert Path(other.log.path).exists()
    other.log.finish("done")


def test_missing_session_is_not_recovered_from_old_log():
    run = make_run("deleted")
    close_as_crash(run)
    assert runs.recover_interrupted_runs(Sessions()) == []
    assert not Path(run.log.path).exists()


def test_lookup_failure_preserves_log_and_recovers_other_sessions():
    failed = make_run("temporary_db_failure")
    close_as_crash(failed)
    good = make_run()
    close_as_crash(good)
    class FlakySessions(Sessions):
        def get_session(self, sid):
            if sid == "temporary_db_failure":
                raise RuntimeError("database unavailable")
            return super().get_session(sid)
    recovered = runs.recover_interrupted_runs(FlakySessions())
    assert [row["run_id"] for row in recovered] == [good.run_id]
    assert Path(failed.log.path).exists()
    assert runs._read_log(failed.log.path)["status"] == "running"


def test_second_restart_keeps_warnings_saved_by_first_restart():
    old = make_run(timestamp=1)
    runs.record_tool_effect("session", call_id="same", tool="send_email", effect_class="write", state="pending")
    close_as_crash(old)
    sessions = Sessions()
    runs.recover_interrupted_runs(sessions)
    runs._INTERRUPTED.clear()
    new = make_run(timestamp=2)
    runs.record_tool_effect("session", call_id="same", tool="send_email", effect_class="write", state="pending")
    close_as_crash(new)
    runs.recover_interrupted_runs(sessions)
    assert len(sessions.history) == 2
    assert {effect["run_id"] for effect in sessions.history[-1].metadata["unknown_effects"]} == {old.run_id, new.run_id}


def test_cleanup_trashes_log_and_sidecar_together(tmp_path, monkeypatch):
    from src import cleanup_service
    monkeypatch.setattr(cleanup_service, "_data_dir", lambda: str(tmp_path))
    run = make_run()
    run.log.finish("done")
    then = time.time() - 61 * 86400
    os.utime(run.log.path, (then, then))
    moved = []
    monkeypatch.setattr(cleanup_service, "move_to_trash", lambda path, **kw: moved.append(path))
    cleanup_service.runs_log_retention(dry_run=False)
    assert moved == [run.log.path, run.log.path + ".status"]


def test_stale_sidecar_does_not_hide_running_log():
    run = make_run()
    close_as_crash(run)
    Path(run.log.path + ".status").write_text("done 0")
    assert runs.recover_interrupted_runs(Sessions())[0]["run_id"] == run.run_id


def test_session_delete_hook_purges_terminal_history_and_orphan_sidecar():
    from routes.session_routes import _stop_runs_for_deleted_sessions
    run = make_run()
    run.log.finish("done")
    runs._RUNS.clear()
    extra = Path(runs._run_log_path("session", "f" * 32) + ".status")
    extra.write_text("done 0")
    _stop_runs_for_deleted_sessions(["session"])
    assert runs._session_log_paths("session") == []
    assert not Path(run.log.path + ".status").exists() and not extra.exists()


def test_latest_selection_does_not_open_historical_headers(monkeypatch):
    created = [make_run(timestamp=number + 1) for number in range(30)]
    for run in created:
        run.log.finish("done")
    runs._RUNS.clear()
    opened = []
    original = runs._log_header
    def record(path):
        opened.append(path)
        return original(path)
    monkeypatch.setattr(runs, "_log_header", record)
    assert runs._log_path("session") == created[-1].log.path
    assert opened == [created[-1].log.path]
    opened.clear()
    assert runs._trace_log_paths(None) == [created[-1].log.path]
    assert opened == [created[-1].log.path]


def test_purge_torn_header_and_sidecar_without_authorizing_reader():
    run = make_run(timestamp=2)
    run.log.orphan()
    Path(run.log.path).write_text('{"session_id":')
    Path(run.log.path + ".status").write_text("running 0")
    other = make_run("another", timestamp=3)
    other.log.finish("done")
    assert runs._log_path("session") != run.log.path
    runs.purge_session_logs("session")
    assert not Path(run.log.path).exists()
    assert not Path(run.log.path + ".status").exists()
    assert Path(other.log.path).exists()


def test_cleanup_preserves_large_inconclusive_running_log(tmp_path, monkeypatch):
    from src import cleanup_service
    monkeypatch.setattr(cleanup_service, "_data_dir", lambda: str(tmp_path))
    run = make_run()
    close_as_crash(run)
    with open(run.log.path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "text", "text": "x" * 100000}) + "\n")
    then = time.time() - 61 * 86400
    os.utime(run.log.path, (then, then))
    assert runs._peek_status(run.log.path) is None
    moved = []
    monkeypatch.setattr(cleanup_service, "move_to_trash", lambda path, **kw: moved.append(path))
    cleanup_service.runs_log_retention(dry_run=False)
    assert moved == []


def test_legacy_collision_never_reads_or_purges_other_session():
    path = Path(runs._legacy_log_path("a/b"))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"session_id": "a_b", "status": "done"}) + "\n")
    assert runs._log_path("a/b") != str(path)
    runs.purge_session_logs("a/b")
    assert path.exists()


def test_startup_retains_old_unreadable_or_inconclusive_log():
    path = Path(runs._run_log_path("session", "f" * 32))
    path.parent.mkdir(parents=True)
    path.write_text('{"session_id":')
    then = time.time() - 49 * 3600
    os.utime(path, (then, then))
    assert runs.recover_interrupted_runs(Sessions()) == []
    assert path.exists()


def test_corrupt_latest_never_falls_back_to_older_or_legacy_trace():
    old = make_run(timestamp=1)
    old.log.finish("done")
    legacy = Path(runs._legacy_log_path("session"))
    legacy.write_bytes(Path(old.log.path).read_bytes())
    new = make_run(timestamp=2)
    new.log.orphan()
    Path(new.log.path).write_text('{"session_id":')
    runs._RUNS.clear()
    assert not Path(runs._log_path("session")).exists()
    assert runs.trace_for_call("call_1_0", session_id="session")["events"] == []
    assert runs._trace_log_paths(None) == []
    assert Path(old.log.path).exists() and legacy.exists()


def test_failed_message_store_preserves_log_and_retry_does_not_duplicate():
    run = make_run()
    close_as_crash(run)
    class FailingStore(Sessions):
        def save_sessions(self):
            self.saved += 1
            if self.saved == 1:
                raise OSError("store unavailable")
    sessions = FailingStore()
    assert runs.recover_interrupted_runs(sessions) == []
    assert runs._read_log(run.log.path)["status"] == "running"
    assert len(sessions.history) == 1
    recovered = runs.recover_interrupted_runs(sessions)
    assert len(recovered) == 1 and recovered[0]["saved_message"]
    assert sessions.saved == 2 and len(sessions.history) == 1
    assert runs._read_log(run.log.path)["status"] == "interrupted"


def test_failed_message_store_new_manager_recovers_from_disk():
    run = make_run()
    close_as_crash(run)
    class FailingStore(Sessions):
        def save_sessions(self):
            raise OSError("store unavailable")
    assert runs.recover_interrupted_runs(FailingStore()) == []
    fresh = Sessions()
    assert runs.recover_interrupted_runs(fresh)[0]["saved_message"]
    assert len(fresh.history) == 1


@pytest.mark.parametrize("new_manager", [False, True])
def test_sqlite_commit_failure_retry_and_restart_do_not_duplicate(tmp_path, monkeypatch, new_manager):
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    import core.database as database
    import core.session_manager as manager_module
    engine = create_engine("sqlite:///" + (tmp_path / "recovery.db").as_posix())
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(manager_module, "SessionLocal", factory)
    with factory() as db:
        db.add(database.Session(id="session", name="test", endpoint_url="local", model="test"))
        db.commit()
    def manager():
        instance = manager_module.SessionManager.__new__(manager_module.SessionManager)
        instance.sessions = {}
        return instance
    def reject_message_commit(db):
        if db.query(database.ChatMessage).count():
            raise OSError("synthetic commit failure")
    instance = manager()
    run = make_run()
    close_as_crash(run)
    original = Path(run.log.path).read_bytes()
    event.listen(factory, "before_commit", reject_message_commit)
    try:
        assert runs.recover_interrupted_runs(instance) == []
        assert runs._read_log(run.log.path)["status"] == "running"
        with factory() as db:
            assert db.query(database.ChatMessage).count() == 0
    finally:
        event.remove(factory, "before_commit", reject_message_commit)
    if new_manager:
        instance = manager()
    assert runs.recover_interrupted_runs(instance)[0]["saved_message"]
    with factory() as db:
        assert db.query(database.ChatMessage).count() == 1
        persisted = db.query(database.ChatMessage).one()
        assert json.loads(persisted.meta_data)["timestamp"]
        assert instance.get_session("session").history[-1].metadata["timestamp"]
        first_id = persisted.id
    # Crash window: message committed, terminal marker not yet durable.
    Path(run.log.path).write_bytes(original)
    Path(run.log.path + ".status").unlink()
    runs._INTERRUPTED.clear()
    assert runs.recover_interrupted_runs(manager())[0]["saved_message"]
    with factory() as db:
        assert db.query(database.ChatMessage).count() == 1
        assert db.query(database.ChatMessage).one().id == first_id
    engine.dispose()


def test_single_recovered_effect_carries_run_identity():
    run = make_run()
    runs.record_tool_effect("session", call_id="same", tool="send_email", effect_class="write", state="pending")
    close_as_crash(run)
    recovered = runs.recover_interrupted_runs(Sessions())
    assert recovered[0]["unknown_effects"][0]["run_id"] == run.run_id
