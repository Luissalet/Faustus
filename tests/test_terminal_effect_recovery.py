"""Terminal effect notices use confirmed SQLite commits, never resend."""
import json
import os
import time
from pathlib import Path

import pytest
from core import database, session_manager
from core.models import ChatMessage
from src import agent_runs as ar, constants
from tests.test_subagent_causal_identity import history


@pytest.fixture
def store(history, tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(ar, "_INTERRUPTED", {})
    history.create_session(session_id="terminal-fixture", name="Fixture",
                           endpoint_url="https://synthetic.invalid", model="fixture")
    return history


def journal(status="done", effect_state="pending", *, saved=False):
    run = ar._Run(label="Synthetic terminal fixture")
    run.log = ar._RunLog("terminal-fixture", run)
    ar._EffectRecorder(run).record(call_id="native-effect", tool="send_email", effect_class="write",
        state=effect_state, durable=True, intent={"owner": "synthetic-owner", "arguments_sha256": "synthetic-sha"})
    if saved:
        ar._publish(run, 'data: {"type":"message_saved"}\n\n')
    run.status = status
    run.log.finish(status)
    return run, Path(run.log.path)


def latest(store, run_id):
    reopened = session_manager.SessionManager().get_session("terminal-fixture")
    return [m for m in reopened.history if m.role == "assistant" and (m.metadata or {}).get("run_id") == run_id]


@pytest.mark.parametrize("status", ["done", "error", "stopped", "waiting_user", "interrupted"])
@pytest.mark.parametrize("effect_state", ["pending", "partial", "unknown"])
def test_terminal_state_preserved_and_notice_committed_once(store, status, effect_state):
    before = dict(ar._RUNS)
    run, path = journal(status, effect_state)
    result = ar.recover_interrupted_runs(store)
    assert len(result) == 1 and result[0]["status"] == status
    assert ar._read_log(str(path))["status"] == status
    assert ar._read_status_sidecar(str(path)) == status
    message = latest(store, run.run_id)[0]
    assert message.metadata["terminal_effect_recovery"]["terminal_status"] == status
    assert message.metadata["unknown_effects"][0]["idempotency_key"] == run.run_id + ":native-effect"
    assert not message.metadata.get("interrupted") and not message.metadata.get("stopped")
    assert "restart" not in ar.unknown_effects_system_block(store.get_session("terminal-fixture"))
    assert ar.recover_interrupted_runs(session_manager.SessionManager()) == []
    assert len(latest(store, run.run_id)) == 1
    assert ar._RUNS == before


def test_existing_saved_message_content_and_metadata_survive(store):
    run, path = journal(saved=True)
    existing = ChatMessage("assistant", "Original answer stays exactly.", metadata={"run_id": run.run_id,
        "sources": ["synthetic source"], "custom": {"keep": "original"}, "unknown_effects": [
        {"idempotency_key": run.run_id + ":native-effect", "call_id": "native-effect", "custom": "keep"},
        {"idempotency_key": "another-run:other", "call_id": "other"}]})
    assert store.persist_recovered_message("terminal-fixture", existing)
    ar.recover_interrupted_runs(store)
    message = latest(store, run.run_id)[0]
    assert message.content == existing.content
    assert message.metadata["sources"] == ["synthetic source"]
    assert message.metadata["custom"] == {"keep": "original"}
    assert len(message.metadata["unknown_effects"]) == 2
    assert message.metadata["unknown_effects"][0]["custom"] == "keep"
    assert ar._read_log(str(path)).get("effect_recovery")


@pytest.mark.parametrize("state", ["confirmed", "failed"])
def test_resolved_effect_has_no_notice(store, state):
    run, path = journal(effect_state=state)
    assert ar.recover_interrupted_runs(store) == []
    assert latest(store, run.run_id) == []
    assert not ar._read_log(str(path)).get("effect_recovery")


def test_db_failure_preserves_log_and_inmemory_message_for_retry(store, monkeypatch):
    run, path = journal()
    existing = ChatMessage("assistant", "Original.", metadata={"run_id": run.run_id, "keep": True})
    assert store.persist_recovered_message("terminal-fixture", existing)
    before_content, before_meta = existing.content, dict(existing.metadata)
    os.utime(path, (time.time() - 100 * 3600,) * 2)
    original = store.persist_recovered_message
    def fail(*args):
        raise OSError("synthetic SQLite commit failure")
    monkeypatch.setattr(store, "persist_recovered_message", fail)
    assert ar.recover_interrupted_runs(store) == []
    assert path.exists() and not ar._read_log(str(path)).get("effect_recovery")
    assert existing.content == before_content and existing.metadata == before_meta
    assert not latest(store, run.run_id)[0].metadata.get("unknown_effects")
    monkeypatch.setattr(store, "persist_recovered_message", original)
    assert ar.recover_interrupted_runs(store)[0]["saved_message"]
    assert not path.exists()  # same existing retention deadline, only after ack
    assert latest(store, run.run_id)[0].metadata["unknown_effects"]


def test_receipt_fsync_failure_after_commit_retries_without_duplicate(store, monkeypatch):
    run, path = journal()
    sync = ar.os.fsync
    def fail(fd):
        raise OSError("synthetic receipt sync failure")
    monkeypatch.setattr(ar.os, "fsync", fail)
    result = ar.recover_interrupted_runs(store)
    assert result[0]["saved_message"]
    assert len(latest(store, run.run_id)) == 1
    assert path.exists() and not ar._read_log(str(path)).get("effect_recovery")
    monkeypatch.setattr(ar.os, "fsync", sync)
    assert ar.recover_interrupted_runs(session_manager.SessionManager())[0]["saved_message"]
    assert len(latest(store, run.run_id)) == 1
    assert ar._read_log(str(path)).get("effect_recovery")
    assert ar.recover_interrupted_runs(store) == []


def test_new_pending_after_receipt_changes_digest_and_merges(store):
    run, path = journal()
    ar.recover_interrupted_runs(store)
    previous = ar._read_log(str(path))["effect_recovery"]["digest"]
    info = ar._read_log(str(path))
    event = "data: " + json.dumps({"type": "tool_effect", "call_id": "next-call", "tool": "send_email",
        "effect_class": "write", "state": "unknown", "idempotency_key": run.run_id + ":next-call"}) + "\n\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"seq": len(info["events"]), "ev": event}) + "\n")
    ar.recover_interrupted_runs(store)
    assert ar._read_log(str(path))["effect_recovery"]["digest"] != previous
    assert len(latest(store, run.run_id)) == 1
    assert len(latest(store, run.run_id)[0].metadata["unknown_effects"]) == 2
    assert ar.recover_interrupted_runs(store) == []


def test_lookup_failure_preserves_log_without_purge(store, monkeypatch):
    run, path = journal()
    def fail(*args):
        raise RuntimeError("synthetic session lookup failure")
    monkeypatch.setattr(store, "get_session", fail)
    assert ar.recover_interrupted_runs(store) == []
    assert path.exists() and not ar._read_log(str(path)).get("effect_recovery")


def test_absent_confirmed_commit_or_manager_never_acknowledges(store):
    run, path = journal()
    assert ar.recover_interrupted_runs(None) == []
    assert path.exists() and not ar._read_log(str(path)).get("effect_recovery")


def test_real_sqlite_commit_failure_does_not_mutate_history(store):
    from sqlalchemy import event
    run, path = journal()
    existing = ChatMessage("assistant", "Original persisted.", metadata={"run_id": run.run_id, "source": "fixture"})
    assert store.persist_recovered_message("terminal-fixture", existing)
    before = dict(existing.metadata)
    with database.SessionLocal() as session:
        engine = session.get_bind()
    def fail(connection):
        raise OSError("synthetic real commit failure")
    event.listen(engine, "commit", fail)
    try:
        assert ar.recover_interrupted_runs(store) == []
    finally:
        event.remove(engine, "commit", fail)
    assert existing.metadata == before
    assert store.get_session("terminal-fixture").history[-1] is existing
    assert not latest(store, run.run_id)[0].metadata.get("unknown_effects")
    assert path.exists() and not ar._read_log(str(path)).get("effect_recovery")
    assert ar.recover_interrupted_runs(store)[0]["saved_message"]


def test_missing_session_purges_without_recreating(store, monkeypatch):
    run, path = journal()
    monkeypatch.setattr(store, "get_session", lambda sid: None)
    assert ar.recover_interrupted_runs(store) == []
    assert not path.exists()


def test_unconfirmed_commit_result_has_no_receipt_or_prune(store, monkeypatch):
    run, path = journal()
    monkeypatch.setattr(store, "persist_recovered_message", lambda *a: False)
    os.utime(path, (time.time() - 100 * 3600,) * 2)
    assert ar.recover_interrupted_runs(store) == []
    assert path.exists() and not ar._read_log(str(path)).get("effect_recovery")
    assert latest(store, run.run_id) == []
