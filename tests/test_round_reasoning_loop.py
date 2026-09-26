"""The follow-up reasoning cap reaches the model call: round 1 keeps the
full budget, a round after clean tool results gets the cap, and a round
after a failed tool keeps the full budget (src/round_reasoning.py)."""

import json

import src.agent_loop as al
from tests.test_agent_harness_loop import _collect, _events


def _run(monkeypatch, tmp_path, tool_results, cap=1024):
    settings = {"agent_followup_reasoning_budget": cap}
    monkeypatch.setattr(al, "get_setting", lambda k, d=None: settings.get(k, d), raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    results = list(tool_results)

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, dict(results.pop(0) if results else {"output": "ok", "exit_code": 0}))
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    seen = []
    script = [
        [{"name": "read_file", "arguments": json.dumps({"path": "a.py"})}],
        [{"name": "read_file", "arguments": json.dumps({"path": "b.py"})}],
        [{"name": "read_file", "arguments": json.dumps({"path": "c.py"})}],
        "En a.py x vale 1; no se cambió nada.",
    ]

    async def _fake_stream(_c, messages, **kwargs):
        seen.append((kwargs.get("gen_overrides") or {}).get("reasoning_budget"))
        item = script[min(len(seen) - 1, len(script) - 1)]
        if isinstance(item, str):
            yield f'data: {json.dumps({"delta": item})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        else:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": item})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "tool_calls"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:8081/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "¿Cuánto vale x en a.py?"}],
        max_rounds=6, relevant_tools={"read_file"}, workspace=str(tmp_path),
        gen_overrides={"think": True, "reasoning_budget": 4096},
        think_mode={"mode": "think", "source": "rule"},
    )))
    return seen


def test_clean_followups_are_capped_and_failures_keep_full_budget(tmp_path, monkeypatch):
    seen = _run(monkeypatch, tmp_path, [
        {"output": "x = 1", "exit_code": 0},
        {"error": "file locked", "exit_code": 1},
        {"output": "x = 1", "exit_code": 0},
    ])
    assert seen[0] == 4096          # first round: full budget
    assert seen[1] == 1024          # after a clean read: capped
    assert seen[2] == 4096          # after a failed read: full budget again
    assert seen[3] == 1024


def test_off_by_default(tmp_path, monkeypatch):
    seen = _run(monkeypatch, tmp_path, [], cap=0)
    assert seen and all(b == 4096 for b in seen)
