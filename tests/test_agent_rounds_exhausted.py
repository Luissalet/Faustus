"""Regression: stream_agent_loop emits `rounds_exhausted` only when the round
cap is hit while still working, and NOT on a normal finish.

The decision is a `for/else` in the loop: the `else` runs only if no `break`
fired (break = done / budget / error). A refactor that adds a stray break or
return, or moves the done-break, could silently flip this. See PR #1999 / #1997.
"""

import asyncio
import json

import src.agent_loop as al


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _types(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch_common(monkeypatch):
    # Skip RAG/tool-index, MCP, and settings lookups; keep the real loop body,
    # _resolve_tool_blocks, and parse_tool_blocks.
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _run_loop(monkeypatch, round_text, max_rounds=2):
    async def _fake_stream(_candidates, messages, **kwargs):
        yield f'data: {json.dumps({"delta": round_text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do a long multi-step task"}],
        max_rounds=max_rounds,
        relevant_tools={"bash"},
    )
    return _types(_collect(gen))


def test_emits_rounds_exhausted_when_cap_hit_mid_task(monkeypatch):
    _patch_common(monkeypatch)
    # Use a system-owned interaction result so this remains a loop-control test:
    # Bash output is workspace-derived and now correctly pauses for exact user
    # approval before a later Bash call.
    events = _run_loop(
        monkeypatch,
        '```update_plan\n{"plan":"- [ ] keep going"}\n```',
        max_rounds=2,
    )
    assert any(e.get("type") == "rounds_exhausted" for e in events), events


def test_no_rounds_exhausted_on_normal_finish(monkeypatch):
    _patch_common(monkeypatch)
    # A plain answer (no tool block) -> done-break on round 1 -> no event.
    events = _run_loop(monkeypatch, "All done, here is your answer.", max_rounds=2)
    assert not any(e.get("type") == "rounds_exhausted" for e in events), events


def test_workspace_acknowledgement_without_tools_is_forced_to_act(monkeypatch, tmp_path):
    _patch_common(monkeypatch)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(
        al, "_agent_route_tool_mode", lambda *args, **kwargs: (True, False, True),
        raising=False,
    )
    round_no = 0

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal round_no
        round_no += 1
        if round_no == 1:
            yield f'data: {json.dumps({"delta": "Reference context received."})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        elif round_no == 2:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": [{"name": "read_file", "arguments": json.dumps({"path": "app.py"})}]})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "tool_calls"})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "Inspected the project and reported the result."})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    events = _types(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3.8:27b-q8_0",
        [{"role": "user", "content": "Implement the fixes in the zip for the project"}],
        workspace=str(tmp_path),
        max_rounds=3,
        relevant_tools={"read_file", "apply_patch", "python"},
    )))

    assert any(e.get("type") == "harness_check" and e.get("status") == "no_action" for e in events)
    assert round_no >= 2


def test_emits_intent_nudge_exhausted_when_cap_is_exhausted(monkeypatch):
    _patch_common(monkeypatch)

    events = _run_loop(monkeypatch, "Let me check the logs", max_rounds=5)

    guard = next((e for e in events if e.get("type") == "intent_nudge_exhausted"), None)
    assert guard is not None, events
    assert guard["reason"] == "intent_without_action_nudge_cap"
    assert guard["nudges"] == 2


def test_emits_loop_breaker_triggered_when_loop_breaker_trips(monkeypatch):
    _patch_common(monkeypatch)

    events = _run_loop(
        monkeypatch,
        '```update_plan\n{"plan":"- [ ] keep going"}\n```',
        max_rounds=6,
    )

    guard = next((e for e in events if e.get("type") == "loop_breaker_triggered"), None)
    assert guard is not None, events
    assert guard["reason"] == "loop_breaker_stall"


def test_loop_breaker_suppresses_only_repeated_tool_and_continues(monkeypatch):
    """A loop recovery must not turn an unfinished task into a forced answer."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(
        al, "_agent_route_tool_mode", lambda *args, **kwargs: (True, False, False),
        raising=False,
    )
    rounds = [
        [{"name": "update_plan", "arguments": json.dumps({"plan": "- [ ] implement"})}],
        [{"name": "update_plan", "arguments": json.dumps({"plan": "- [ ] implement"})}],
        [{"name": "update_plan", "arguments": json.dumps({"plan": "- [ ] implement"})}],
        [{"name": "read_file", "arguments": json.dumps({"path": "app.py"})}],
        "Finished after continuing with the remaining tools.",
    ]
    round_no = 0
    schemas_by_round = []
    executed = []

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal round_no
        schemas_by_round.append({
            schema.get("function", {}).get("name")
            for schema in (kwargs.get("tools") or [])
        })
        item = rounds[round_no]
        round_no += 1
        if isinstance(item, str):
            yield f'data: {json.dumps({"delta": item})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        else:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": item})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "tool_calls"})}\n\n'
        yield "data: [DONE]\n\n"

    async def _fake_exec(block, *args, **kwargs):
        executed.append(block.tool_type)
        return block.tool_type, {"output": "ok", "exit_code": 0}

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    events = _types(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3.8:27b-q8_0",
        [{"role": "user", "content": "Implement the project"}],
        max_rounds=7,
        relevant_tools={"update_plan", "read_file"},
    )))

    guard = next(e for e in events if e.get("type") == "loop_breaker_triggered")
    assert guard["repeated_tools"] == ["update_plan"]
    assert "update_plan" not in schemas_by_round[3]
    assert "read_file" in schemas_by_round[3]
    assert executed == ["update_plan", "update_plan", "read_file"]


def test_repeated_probe_narration_does_not_count_as_progress(monkeypatch):
    """Qwen must not evade the loop breaker by narrating the same tool call."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(
        al, "_agent_route_tool_mode", lambda *args, **kwargs: (True, False, False),
        raising=False,
    )
    rounds = 0
    executed = []

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds <= 3:
            yield f'data: {json.dumps({"delta": "Voy a diagnosticarlo otra vez."})}\n\n'
            yield f'data: {json.dumps({"type": "tool_calls", "calls": [{"name": "update_plan", "arguments": json.dumps({"plan": "- [ ] diagnose"})}]})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "tool_calls"})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "No repetiré el diagnóstico."})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "finish_reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"

    async def _fake_exec(block, *args, **kwargs):
        executed.append(block.tool_type)
        return block.tool_type, {"output": "same result", "exit_code": 0}

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    events = _types(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3.8:27b-q8_0",
        [{"role": "user", "content": "Fix the bug in app.py"}],
        max_rounds=6,
        relevant_tools={"update_plan", "edit_file", "read_file"},
    )))

    assert any(e.get("type") == "loop_breaker_triggered" for e in events), (events, executed, rounds)
    assert executed == ["update_plan", "update_plan"]
    summary = next(e for e in events if e.get("type") == "harness_summary")
    assert summary["data"]["stop_reason"] == "complete"


def test_long_calls_with_same_prefix_are_not_a_loop(monkeypatch):
    """Regression for Silhouettes: exploratory Python calls shared long import
    prefixes but differed later. The loop breaker must compare the full payload,
    not the first 120 characters used for display."""
    _patch_common(monkeypatch)
    prefix = "x" * 140
    calls = iter([
        f'```update_plan\n{{"plan":"- [ ] {prefix} probe {n}"}}\n```'
        for n in range(4)
    ] + ["All done."])

    async def _fake_stream(_candidates, messages, **kwargs):
        yield f'data: {json.dumps({"delta": next(calls)})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1",
        "m",
        [{"role": "user", "content": "do a long multi-step task"}],
        max_rounds=6,
        relevant_tools={"update_plan"},
    )))

    assert not any(e.get("type") == "loop_breaker_triggered" for e in events), events
    summary = next(e for e in events if e.get("type") == "harness_summary")
    assert summary["data"]["stop_reason"] == "complete"


def test_attachment_body_is_context_not_tool_routing_intent():
    prompt = (
        "Implement this plan to improve the project\n"
        "=== File: implementation.md ===\n"
        "# Plan\nConfigure the model server. Edit images. Open the UI panel. "
        "Create calendar tasks."
    )

    intent = al._classify_agent_request([], prompt)

    assert intent["retrieval_query"] == "Implement this plan to improve the project"
    assert intent["domains"] == {"files"}
    assert intent["low_signal"] is False
    assert not al._detect_admin_intent([{"role": "user", "content": prompt}])


def test_cosmetic_python_probe_variants_are_stopped_as_one_semantic_loop(monkeypatch):
    _patch_common(monkeypatch)
    scripted = iter([
        f"```python\nimport vtracer\nprint({n})\n```" for n in range(5)
    ] + ["```python\nimport vtracer\nprint('again')\n```",
         "Continued with the implementation."])

    async def _fake_stream(_candidates, messages, **kwargs):
        yield f'data: {json.dumps({"delta": next(scripted)})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    events = _types(_collect(al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3.8:27b-q8_0",
        [{"role": "user", "content": "Implement the project"}],
        max_rounds=7,
        relevant_tools={"python", "apply_patch"},
    )))

    guard = next(e for e in events if e.get("type") == "loop_breaker_triggered")
    assert guard["repeated_tools"] == ["python"]
    assert "diagnostics without advancing" in guard["detail"]
    assert guard["round"] == 5
    redirected = next(e for e in events if e.get("type") == "loop_retry_redirected")
    assert redirected["tool"] == "python"
    summary = next(e for e in events if e.get("type") == "harness_summary")
    assert summary["data"]["stop_reason"] == "complete"
