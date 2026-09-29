"""Replay can coalesce one call's progress, never a different call's evidence."""
import json

import pytest

from src import agent_runs


def progress(call_id=None, tail="first"):
    payload = {"type": "tool_progress", "tool": "bash", "round": 1, "tail": tail}
    if call_id is not None:
        payload["call_id"] = call_id
    return "data: " + json.dumps(payload) + "\n\n"


@pytest.fixture
def run(tmp_path, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    run = agent_runs._Run()
    run.log = agent_runs._RunLog("h08", run)
    monkeypatch.setattr(agent_runs, "_RUNS", {"h08": run})
    monkeypatch.setattr(agent_runs, "_receipt_for_call_id", lambda call_id: None)
    monkeypatch.setattr(agent_runs, "_artifacts_for_call_id", lambda call_id: [])
    yield run
    run.log.finish("done")


@pytest.mark.parametrize("ids", [("call-a", "call-b"), ("call|a", "call|b")])
def test_distinct_calls_survive_buffer_disk_and_trace(run, ids):
    for call_id in ids:
        agent_runs._publish(run, progress(call_id))
    for events in (run.buffer, agent_runs._read_log(run.log.path)["events"]):
        rows = [json.loads(event[6:]) for event in events]
        assert [row["call_id"] for row in rows] == list(ids)
        assert [row["sequence"] for row in rows] == [1, 2]
    agent_runs._RUNS.clear()  # trace must also survive loss of the live buffer
    for call_id in ids:
        trace = agent_runs.trace_for_call(call_id, session_id="h08")
        assert trace["found"]
        assert len(trace["events"]) == 1
        assert trace["events"][0]["call_id"] == call_id


def test_same_call_still_coalesces_and_legacy_without_id_remains_compatible(run):
    for call_id in ("call-a", None):
        agent_runs._publish(run, progress(call_id, "old"))
        agent_runs._publish(run, progress(call_id, "latest"))
    for events in (run.buffer, agent_runs._read_log(run.log.path)["events"]):
        rows = [json.loads(event[6:]) for event in events]
        assert len(rows) == 2
        assert [row["tail"] for row in rows] == ["latest", "latest"]
        assert [row["sequence"] for row in rows] == [1, 2]
        assert rows[0]["call_id"] == "call-a" and "call_id" not in rows[1]


def test_effect_events_are_never_replaced_by_progress_compaction(run):
    agent_runs.record_tool_effect("h08", call_id="call-a", tool="send_email", effect_class="write", state="pending", durable=True)
    agent_runs._publish(run, progress("call-a", "old"))
    agent_runs._publish(run, progress("call-a", "latest"))
    agent_runs.record_tool_effect("h08", call_id="call-a", tool="send_email", effect_class="write", state="unknown")
    events = agent_runs._read_log(run.log.path)["events"]
    assert [json.loads(event[6:])["type"] for event in events] == ["tool_effect", "tool_progress", "tool_effect"]
    assert agent_runs._partial_from_events(events)["unknown_effects"][0]["state"] == "unknown"
