"""Agent loop state: phases from real stream events, steps and the practice checklist."""
import json

from src import agent_loop_state as loop


def _practice(snapshot, pid):
    return next(p for p in snapshot["practices"] if p["id"] == pid)


def test_phases_follow_events_and_laps_follow_rounds():
    state = loop.LoopState("s1", "r1", 100.0)
    assert state.snapshot(100.0)["phase"] == "plan"
    state.feed({"delta": "thinking about it", "thinking": True}, 101.0)
    state.feed({"type": "tool_start", "tool": "bash", "command": "pytest -q", "round": 1}, 102.0)
    snap = state.snapshot(102.5)
    assert (snap["phase"], snap["tool"], snap["lap"]) == ("act", "bash", 1)
    state.feed({"type": "tool_output", "tool": "bash", "exit_code": 0, "output": "3 passed"}, 104.0)
    assert state.snapshot(104.0)["phase"] == "observe"
    state.feed({"type": "agent_step", "round": 2}, 105.0)
    snap = state.snapshot(105.0)
    assert (snap["phase"], snap["lap"]) == ("observe", 2)
    state.feed({"delta": "\n\n", "thinking": False}, 105.2)
    assert state.snapshot(105.2)["phase"] == "observe"
    state.feed({"delta": "it passed", "thinking": False}, 105.5)
    assert state.snapshot(105.5)["phase"] == "plan"
    state.feed({"type": "metrics", "data": {"input_tokens": 1200, "output_tokens": 80}}, 106.0)
    state.finish("done", 107.0)
    snap = state.snapshot(200.0)
    assert snap["phase"] == "done" and not snap["running"]
    assert snap["elapsed_s"] == 7.0
    assert snap["tokens"] == {"input": 1200, "output": 80}
    assert [s["kind"] for s in snap["steps"]] == ["plan", "tool", "result", "plan", "end"]


def test_no_movement_without_events():
    state = loop.LoopState("s1", "r1", 0.0)
    state.feed({"type": "tool_start", "tool": "bash", "command": "sleep 60"}, 1.0)
    assert state.snapshot(500.0)["phase"] == "act"


def test_practices_marked_from_what_the_calls_ran():
    state = loop.LoopState("s1", "r1", 0.0)
    calls = [
        ("bash", "python -m pytest tests/test_x.py -q", 0),
        ("browser_navigate", "http://127.0.0.1:7012", 0),
        ("edit_file", "FAUSTUS.md", 0),
        ("bash", 'git add src/x.py FAUSTUS.md && git commit --author="Luissalet <l@x>" -m "Add x"', 0),
        ("bash", "agora.cmd --space mejoras submit 12 --commits abc", 0),
    ]
    for i, (tool, command, code) in enumerate(calls):
        state.feed({"type": "tool_start", "tool": tool, "command": command}, i + 0.1)
        state.feed({"type": "tool_output", "tool": tool, "exit_code": code}, i + 0.5)
    snap = state.snapshot(10.0)
    assert {p["id"]: p["state"] for p in snap["practices"]} == {
        "tests": "ok", "browser": "ok", "docs": "ok", "commit": "ok", "no_evidence": "ok", "agora": "ok"}
    assert "pytest" in _practice(snap, "tests")["evidence"]
    assert [s["kind"] for s in snap["steps"]].count("commit") == 1
    assert [s["kind"] for s in snap["steps"]].count("submit") == 1


def test_failures_and_unsettled_identity():
    calls = [
        {"tool": "bash", "command": "npm test", "ok": False},
        {"tool": "bash", "command": "git add shot.png && git commit -m wip", "ok": True},
    ]
    marks = {p["id"]: p for p in loop.judge_practices(calls)}
    assert marks["tests"]["state"] == "fail"
    assert marks["no_evidence"]["state"] == "fail"
    assert marks["commit"]["state"] == "unknown"
    assert marks["browser"]["state"] == "pending" and marks["agora"]["state"] == "pending"
    other = loop.judge_practices([{"tool": "bash", "command": "git commit --author='Someone <s@x>' -m x", "ok": True}])
    assert next(p for p in other if p["id"] == "commit")["state"] == "fail"
    trailer = loop.judge_practices([{"tool": "bash", "command": "git commit -m 'x\n\nCo-Authored-By: Bot'", "ok": True}])
    assert next(p for p in trailer if p["id"] == "commit")["state"] == "fail"


def test_registry_keeps_last_loop_and_runs_feed_it():
    from src import agent_runs

    state = loop.begin("sess-loop", "run-1", 10.0)
    run = agent_runs._Run()
    run.loop = state
    agent_runs._observe_activity(run, "data: " + json.dumps({"type": "tool_start", "tool": "bash", "command": "ls"}) + "\n\n")
    assert loop.snapshot("sess-loop")["phase"] == "act"
    agent_runs._observe_activity(run, "data: " + json.dumps({"delta": "ok", "thinking": False}) + "\n\n")
    assert loop.snapshot("sess-loop")["phase"] == "plan"
    loop.finish(state, "done")
    assert loop.snapshot("sess-loop")["running"] is False
    assert loop.snapshot("never-ran") is None
