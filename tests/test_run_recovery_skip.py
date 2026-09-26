"""Recovery reads only what it uses from a long run log."""
import json

from src import agent_runs


def _ev(obj):
    return "data: " + json.dumps(obj) + "\n\n"


def test_recovery_skips_reasoning_and_heartbeats_but_keeps_the_answer(tmp_path):
    p = tmp_path / "s1.jsonl"
    lines = [json.dumps({"status": "running", "run_id": "r1", "session_id": "s1"})]
    evs = [_ev({"delta": "thinking hard", "thinking": True}),
           _ev({"type": "run_activity", "data": {"phase": "thinking"}}),
           _ev({"delta": "Hola"}),
           _ev({"type": "tool_output", "tool": "read_file", "command": "x", "output": "ok", "exit_code": 0}),
           _ev({"delta": " mundo"})]
    lines += [json.dumps({"seq": i, "ev": e}) for i, e in enumerate(evs)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    full = agent_runs._read_log(str(p))
    lean = agent_runs._read_log(str(p), for_recovery=True)
    assert len(full["events"]) == 5 and len(lean["events"]) == 3
    assert lean["status"] == "running" and lean["run_id"] == "r1"
    assert agent_runs._partial_from_events(lean["events"])["text"] == \
        agent_runs._partial_from_events(full["events"])["text"] == "Hola mundo"
