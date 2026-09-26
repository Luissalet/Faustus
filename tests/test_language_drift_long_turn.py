"""A long turn that drifts into English gets reminded again, spaced out.

Exam run 31 (26-09-2026): the one mid-turn language note fired early; from
then on the round narrations stayed in English for two hours of a Spanish
task. Up to 3 notes now, at least 5 rounds apart.
"""
import json

import src.agent_loop as al
from tests.test_research_streak import _collect, _events, _patch_common

EN = ("The operations are now clearly visible and I will check the remaining lines "
      "and the large figure at the bottom before writing anything else.")


def _stream(monkeypatch, n_rounds, snaps):
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        snaps.append([dict(m) for m in messages])
        i = calls["n"]
        calls["n"] += 1
        if i < n_rounds:
            yield "data: " + json.dumps({"delta": EN + f" Step {i}."}) + "\n\n"
            yield "data: " + json.dumps({"type": "tool_calls", "calls": [
                {"name": "read_file", "arguments": json.dumps({"path": f"n{i}.md"})}]}) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "tool_calls"}) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": "Hecho: he leído todas las notas y este es el resumen."}) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "stop"}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)


def test_spaced_language_notes_in_a_long_turn(tmp_path, monkeypatch):
    _patch_common(monkeypatch, {"agent_web_streak_nudge": 0, "agent_todo_stall_nudge": 0,
                                "agent_no_progress_rounds": 0})
    for i in range(14):
        (tmp_path / f"n{i}.md").write_text(f"nota {i}\n", encoding="utf-8")
    snaps = []
    _stream(monkeypatch, 13, snaps)
    _collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Lee estas notas una por una y resúmemelas en español, por favor."}],
        max_rounds=16, relevant_tools={"read_file"}, workspace=str(tmp_path), session_id="s-lang",
    ))
    last = snaps[-1]
    count = sum(1 for m in last if m.get("_agent_injected") == "reply_language_mismatch")
    assert 2 <= count <= 3, count
