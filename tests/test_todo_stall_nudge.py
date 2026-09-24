"""A plan step stuck in progress while the plan does not change gets one note.

Live, 24-09-2026: "Examinar originales" stayed in progress for 36 rounds of
image questions and the turn ended asking the user whether to go on.
"""
import json

import src.agent_loop as al
import src.agent_tools.coding_tools as coding_tools
from tests.test_research_streak import _FAKE_REPO_FILES, _collect, _events, _patch_common


def _stream(monkeypatch, n_rounds, snapshots):
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        snapshots.append([dict(m) for m in messages])
        i = calls["n"]
        calls["n"] += 1
        if i < n_rounds:
            name = _FAKE_REPO_FILES[i % len(_FAKE_REPO_FILES)]
            yield "data: " + json.dumps({
                "type": "tool_calls",
                "calls": [{"name": "read_file", "arguments": json.dumps({"path": f"notes_{name}.md"})}],
            }) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "tool_calls"}) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": "Done with the step."}) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "stop"}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)


def test_a_stalled_step_is_nudged_once(tmp_path, monkeypatch):
    _patch_common(monkeypatch, {"agent_todo_stall_nudge": 3, "agent_web_streak_nudge": 0})
    todos = [{"content": "Examine the originals", "status": "in_progress"},
             {"content": "Solve", "status": "pending"}]
    monkeypatch.setattr(coding_tools, "load_todos", lambda sid: list(todos))
    snaps = []
    _stream(monkeypatch, 7, snaps)
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "resuelve el acertijo de esta carpeta paso a paso"}],
        max_rounds=12, relevant_tools={"read_file", "todowrite"}, workspace=str(tmp_path),
        session_id="s-stall",
    )))
    stalls = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "todo_stall"]
    assert len(stalls) == 1, [e.get("status") for e in events if e.get("type") == "harness_check"]
    notes = [m["content"] for m in snaps[-1] if m.get("_harness_note")]
    assert any("Examine the originals" in c and "Close it now" in c for c in notes), notes


def test_a_plan_that_moves_is_left_alone(tmp_path, monkeypatch):
    _patch_common(monkeypatch, {"agent_todo_stall_nudge": 3, "agent_web_streak_nudge": 0})
    state = {"n": 0}

    def moving(sid):
        state["n"] += 1
        return [{"content": f"step {state['n']}", "status": "in_progress"}]
    monkeypatch.setattr(coding_tools, "load_todos", moving)
    snaps = []
    _stream(monkeypatch, 7, snaps)
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "resuelve el acertijo de esta carpeta paso a paso"}],
        max_rounds=12, relevant_tools={"read_file", "todowrite"}, workspace=str(tmp_path),
        session_id="s-move",
    )))
    assert not [e for e in events if e.get("type") == "harness_check" and e.get("status") == "todo_stall"]
