"""Rounds of tools that fix nothing get one note, then one insistence.

Exam run 30 (26-09-2026): four and a half hours of reading, probing and
looking at images, alternating sources so no single-shape guard fired, and
no deliverable at the end.
"""
import json

import src.agent_loop as al
import src.agent_tools.coding_tools as coding_tools
from src.progress_watch import ProgressWatch, message
from tests.test_research_streak import _collect, _events, _patch_common


def _read(round_num=1):
    return [{"round": round_num, "tool": "read_file", "ok": True, "kind": "read"}]


def test_nudge_then_insist_then_quiet():
    w = ProgressWatch(rounds=3)
    actions = [w.observe(["read_file"], _read()) for _ in range(9)]
    assert actions == ["none", "none", "nudge", "none", "none", "insist", "none", "none", "none"]
    assert w.longest() == 9


def test_a_write_resets_the_streak():
    w = ProgressWatch(rounds=3)
    w.observe(["grep"], _read()); w.observe(["bash"], _read())
    assert w.observe(["write_file"], [{"tool": "write_file", "ok": True, "kind": "mutation"}]) == "none"
    assert w.streak == 0 and w.history == [2]
    assert [w.observe(["read_file"], _read()) for _ in range(3)][-1] == "nudge"


def test_a_failed_write_is_not_progress():
    w = ProgressWatch(rounds=2)
    w.observe(["read_file"], _read())
    assert w.observe(["write_file"], [{"tool": "write_file", "ok": False, "kind": "mutation"}]) == "nudge"


def test_closing_a_plan_step_is_progress_and_plan_only_rounds_are_neutral():
    w = ProgressWatch(rounds=2)
    todos = [{"content": "a", "status": "in_progress"}]
    w.observe(["read_file"], _read(), todos)
    assert w.observe(["todowrite"], [], todos) == "none" and w.streak == 1   # neutral
    done = [{"content": "a", "status": "completed"}]
    assert w.observe(["todowrite"], [], done) == "none" and w.streak == 0    # a step closed
    w.observe(["read_file"], _read(), done)
    assert w.observe(["read_file"], _read(), done) == "nudge"                # same count: no progress


def test_asking_the_user_is_progress_and_zero_disables():
    w = ProgressWatch(rounds=1)
    assert w.observe(["ask_user"], [], asked_user=True) == "none"
    off = ProgressWatch(rounds=0)
    assert all(off.observe(["read_file"], _read()) == "none" for _ in range(50))


def test_messages_name_the_count_and_the_way_out():
    assert "12 rounds" in message("nudge", 12) and "first version" in message("nudge", 12)
    assert "Stop gathering" in message("insist", 30)


def _stream(monkeypatch, plan, snapshots):
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        snapshots.append([dict(m) for m in messages])
        i = calls["n"]
        calls["n"] += 1
        if i < len(plan):
            name, args = plan[i]
            yield "data: " + json.dumps({
                "type": "tool_calls", "calls": [{"name": name, "arguments": json.dumps(args)}],
            }) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "tool_calls"}) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": "Here is what I found."}) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "stop"}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)


def _run(tmp_path, monkeypatch, plan, settings, sid, **kw):
    _patch_common(monkeypatch, {"agent_todo_stall_nudge": 0, "agent_web_streak_nudge": 0, **settings})
    monkeypatch.setattr(coding_tools, "load_todos", lambda sid: [])
    for i in range(12):
        (tmp_path / f"f{i}.md").write_text(f"note {i}\n", encoding="utf-8")
    snaps = []
    _stream(monkeypatch, plan, snaps)
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "estudia estas notas y escribe un informe en informe.md"}],
        max_rounds=kw.pop("max_rounds", 14), relevant_tools={"read_file", "write_file", "todowrite"}, workspace=str(tmp_path),
        session_id=sid, **kw,
    )))
    checks = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "no_progress"]
    assert len(snaps) > len(plan), "the fake turn ran every planned round"
    return checks, snaps


def test_the_loop_nudges_then_insists(tmp_path, monkeypatch):
    plan = [("read_file", {"path": f"f{i}.md"}) for i in range(8)]
    checks, snaps = _run(tmp_path, monkeypatch, plan, {"agent_no_progress_rounds": 3}, "s-np")
    assert [c["action"] for c in checks] == ["nudge", "insist"], checks
    assert [c["rounds"] for c in checks] == [3, 6]
    notes = [m["content"] for m in snaps[-1] if m.get("_harness_note")]
    assert any("fixed nothing" in c for c in notes) and any("Stop gathering" in c for c in notes)


def test_a_turn_that_writes_is_left_alone(tmp_path, monkeypatch):
    plan = []
    for i in range(8):
        plan.append(("read_file", {"path": f"f{i}.md"}))
        if i % 2:
            plan.append(("write_file", {"path": "informe.md", "content": f"# Informe\n\nparte {i}\n"}))
    checks, _ = _run(tmp_path, monkeypatch, plan, {"agent_no_progress_rounds": 3}, "s-np-writes",
                     max_rounds=20)
    assert not checks


def test_plan_mode_is_exempt(tmp_path, monkeypatch):
    plan = [("read_file", {"path": f"f{i}.md"}) for i in range(8)]
    checks, _ = _run(tmp_path, monkeypatch, plan, {"agent_no_progress_rounds": 3}, "s-np-plan", plan_mode=True)
    assert not checks
