"""A failed outcome may resume after restart without crediting a rule twice."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src import memory_engine as engine


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    yield tmp_path
    engine.clear_injected()
    engine.reset_vector_store()


def test_partial_feedback_resumes_in_a_new_process(store, monkeypatch):
    rules = [engine.add_item(text=text, level="procedural", owner="luis")
             for text in ("Check tests", "Check build")]
    engine.note_injected("restart-run", [rule["id"] for rule in rules])
    original = engine.add_feedback

    def interrupted(item_id, *args, **kwargs):
        if item_id == rules[1]["id"]:
            raise RuntimeError("process interrupted after the first write")
        return original(item_id, *args, **kwargs)

    monkeypatch.setattr(engine, "add_feedback", interrupted)
    assert engine.record_outcome("restart-run", "pass")["ids"] == [rules[0]["id"]]
    script = """
import json, sys
from src import memory_engine as engine
engine.DATA_DIR = sys.argv[1]
engine.set_vector_store(None)
result = engine.record_outcome('restart-run', 'pass')
assert not engine.peek_injected('restart-run')
print(json.dumps(result))
"""
    child = subprocess.run([sys.executable, "-c", script, str(store)],
                           cwd=Path(__file__).resolve().parents[1],
                           capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(child.stdout.splitlines()[-1])["ids"] == [rules[1]["id"]]
    engine._INJECTED.clear()  # Simulate losing this process's obsolete cache.
    assert engine.record_outcome("restart-run", "pass")["applied"] == 0
    events = [engine.get_item(rule["id"])["helpful"] for rule in rules]
    assert all(len(feedback) == 1 for feedback in events)
    assert events[0][0]["event_id"] == events[1][0]["event_id"]


def test_expired_receipt_is_not_restored_after_restart(store, monkeypatch):
    engine.note_injected("expired", ["rule"], now_ts=1000)
    engine._INJECTED.clear()
    monkeypatch.setattr(engine.time, "time", lambda: 1000 + engine.INJECTED_TTL_S + 1)
    assert engine.peek_injected("expired") == []
    with engine._db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM injected_turns").fetchone()[0] == 0


def test_finishing_old_outcome_does_not_consume_new_turn(store, monkeypatch):
    rule = engine.add_item(text="Check tests", level="procedural", owner="luis")
    engine.note_injected("session", [rule["id"]])
    original = engine.add_feedback

    def next_turn(*args, **kwargs):
        result = original(*args, **kwargs)
        engine.note_injected("session", [rule["id"]])
        return result

    monkeypatch.setattr(engine, "add_feedback", next_turn)
    assert engine.record_outcome("session", "pass")["applied"] == 1
    monkeypatch.setattr(engine, "add_feedback", original)
    engine._INJECTED.clear()
    assert engine.record_outcome("session", "pass")["applied"] == 1
    assert len(engine.get_item(rule["id"])["helpful"]) == 2
