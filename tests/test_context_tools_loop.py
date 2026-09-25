"""Loop-level: the model drops a tool result with `context_drop`, the NEXT
request really carries the overflow stub instead of the body, and
`read_overflow` brings the body back — on a native function-calling route and
on a fenced-call route. Only the provider stream, the tool executor (whose
context/read_overflow calls go to the REAL handlers), the MCP manager and
settings are stubbed; everything between is the production agent loop.
"""
from __future__ import annotations

import asyncio
import json
import re
from unittest import mock

import pytest

import src.agent_loop as agent_loop
from src import context_overflow as co
from src.agent_tools import TOOL_HANDLERS
from src.context_engine import store as ce_store

BIG = "".join(f"compiler line {i}: warning something long enough\n" for i in range(400))


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(co, "OVERFLOW_DIR", str(tmp_path / "overflow"))
    ce_store.use_path(str(tmp_path / "ce.db"))
    try:
        yield tmp_path
    finally:
        ce_store.use_path(None)


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _drive(tmp_path, script, *, native, settings=None, user="build it", context_length=200_000):
    """`script(round_index, messages)` returns the model's reply for that
    round: a list of (name, args, call_id) native calls, or a text string."""
    captured = []
    turn_settings = {"agent_context_tools_offer_round": 1}
    turn_settings.update(settings or {})

    async def fake_stream(candidates, messages, **kwargs):
        captured.append({"messages": [dict(m) for m in messages],
                         "tools": [s.get("function", {}).get("name") for s in (kwargs.get("tools") or [])]})
        reply = script(len(captured), messages)
        if isinstance(reply, list):
            yield "data: " + json.dumps({"type": "tool_calls", "calls": [
                {"id": cid, "name": name, "arguments": json.dumps(args)} for name, args, cid in reply
            ]}) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": reply}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        if block.tool_type == "bash":
            return ("bash: make", {"output": BIG, "exit_code": 0})
        handler = TOOL_HANDLERS[block.tool_type]
        ctx = {"session_id": kwargs.get("session_id"), "owner": kwargs.get("owner"),
               "run_id": (kwargs.get("turn_options") or {}).get("run_id")}
        return (block.tool_type, await handler(block.content, ctx))

    patches = [
        mock.patch.object(agent_loop, "stream_llm_with_fallback", fake_stream),
        mock.patch.object(agent_loop, "execute_tool_block", fake_execute),
        mock.patch.object(agent_loop, "get_mcp_manager", lambda: None),
        mock.patch.object(agent_loop, "get_setting", lambda k, d=None: turn_settings.get(k, d)),
        mock.patch.object(agent_loop, "_agent_route_tool_mode", lambda *a, **k: (bool(native), False, True)),
        mock.patch.object(agent_loop, "blocked_tools_for_owner", lambda o: set()),
    ]
    for p in patches:
        p.start()
    try:
        async def run():
            gen = agent_loop.stream_agent_loop(
                endpoint_url="http://127.0.0.1:11434/v1", model="qwen3.6-27b",
                messages=[{"role": "user", "content": user}], headers={},
                workspace=str(tmp_path), owner="admin", session_id="sess-ctx-loop",
                max_rounds=6, context_length=context_length,
                relevant_tools={"bash", "read_overflow"},
                security_gate_bypass=True,
                harness_options={"checkpoints": False, "run_tests": False, "repo_map": False},
            )
            return [c async for c in gen]
        chunks = asyncio.run(run())
    finally:
        for p in patches:
            p.stop()
    return captured, _events(chunks)


def _tool_msg(messages, call_id):
    return next(m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == call_id)


def test_native_drop_changes_next_request_and_read_overflow_restores(stores):
    state = {}

    def script(n, messages):
        if n == 1:
            return [("bash", {"command": "make"}, "call_big")]
        if n == 2:
            assert _tool_msg(messages, "call_big")["content"].count("compiler line") > 100
            return [("context_drop", {"handles": ["call_big"]}, "call_drop")]
        if n == 3:
            stub = _tool_msg(messages, "call_big")["content"]
            state["stub"] = stub
            state["id"] = re.search(r"\[overflow id=([0-9a-f]{64})", stub).group(1)
            state["drop_result"] = _tool_msg(messages, "call_drop")["content"]
            return [("read_overflow", {"content_sha256": state["id"]}, "call_back")]
        if n == 4:
            state["restored"] = _tool_msg(messages, "call_back")["content"]
            return "Done."
        return "Done."

    captured, events = _drive(stores, script, native=True)
    assert len(captured) >= 4
    # Offered: round >= agent_context_tools_offer_round (1 here).
    assert "context_drop" in captured[1]["tools"]
    assert state["stub"].startswith("[overflow id=")
    assert "compiler line 399" not in state["stub"]
    assert "dropped 1 result" in state["drop_result"]
    assert "compiler line 399" in state["restored"]
    ops = [e for e in events if e.get("type") == "context_op"]
    assert ops and ops[0]["op"] == "drop" and ops[0]["handles"] == ["call_big"]
    assert ops[0]["tokens_freed"] > 1000 and ops[0]["overflow_ids"] == [state["id"]]
    harness = [e for e in events if e.get("type") == "metrics"][-1]["data"]["harness"]
    assert harness["context_ops"]["drops"] == 1 and harness["context_ops"]["tokens_freed"] > 1000
    assert harness["total_tokens"] > 0


def test_native_context_tools_are_not_offered_before_the_threshold(stores):
    def script(n, messages):
        return "Done."
    captured, _events_ = _drive(stores, script, native=True,
                                settings={"agent_context_tools_offer_round": 12})
    assert not ({"context_status", "context_drop"} & set(captured[0]["tools"]))


def test_native_user_asking_offers_them_on_round_one(stores):
    def script(n, messages):
        return "Done."
    captured, _e = _drive(stores, script, native=True, user="free up your context and continue",
                          settings={"agent_context_tools_offer_round": 12})
    assert {"context_status", "context_drop", "context_note", "context_pin"} <= set(captured[0]["tools"])


def test_native_note_replaces_result_in_next_request(stores):
    state = {}

    def script(n, messages):
        if n == 1:
            return [("bash", {"command": "make"}, "call_big")]
        if n == 2:
            return [("context_note", {"handles": ["call_big"], "note": "make: 400 warnings, no errors"}, "call_note")]
        if n == 3:
            state["note"] = _tool_msg(messages, "call_big")["content"]
        return "Done."

    _drive(stores, script, native=True)
    assert "make: 400 warnings, no errors" in state["note"]
    assert "[overflow id=" in state["note"] and "compiler line 399" not in state["note"]


def _fenced_results(messages):
    return [m for m in messages if m.get("role") == "user" and "tool execution results" in str(m.get("content"))]


def test_fenced_drop_changes_next_request_and_read_overflow_restores(stores):
    state = {}

    def script(n, messages):
        if n == 1:
            return "```bash\nmake\n```"
        if n == 2:
            assert "compiler line 399" in _fenced_results(messages)[-1]["content"]
            state["offer_note"] = any("context_drop" in str(m.get("content")) and m.get("role") == "user"
                                      for m in messages)
            return '```context_drop\n{"handles": ["r1.0"]}\n```'
        if n == 3:
            first = _fenced_results(messages)[0]["content"]
            state["first"] = first
            state["id"] = re.search(r"\[overflow id=([0-9a-f]{64})", first).group(1)
            return '```read_overflow\n{"content_sha256": "%s"}\n```' % state["id"]
        if n == 4:
            state["restored"] = _fenced_results(messages)[-1]["content"]
        return "Done."

    captured, events = _drive(stores, script, native=False)
    assert state["offer_note"], "fenced route got no note offering the context tools"
    assert "compiler line 399" not in state["first"]
    assert "UNTRUSTED_SOURCE_DATA" in state["first"]           # wrapper intact
    assert "compiler line 399" in state["restored"]
    ops = [e for e in events if e.get("type") == "context_op"]
    assert ops and ops[0]["handles"] == ["r1.0"]



def _nudges(messages):
    return [m.get("content") for m in messages
            if m.get("role") == "user" and "Context is at" in str(m.get("content"))]


def test_nudge_line_once_usage_reaches_the_soft_ceiling(stores):
    seen = {}

    def script(n, messages):
        seen[n] = (_nudges(messages), )
        if n <= 2:
            return [("bash", {"command": f"make {n}"}, f"call_big{n}")]
        return "Done."

    settings = {"agent_context_tools_offer_round": 12, "agent_midturn_compact_enabled": False,
                "agent_tool_result_offload_chars": 10_000_000}
    # The route's window as the loop resolves it (not the caller's hint).
    with mock.patch("src.model_context.budget_context_for_model", lambda *a, **k: 16_000):
        captured, _e = _drive(stores, script, native=True, settings=settings)
    assert not seen[1][0] and "context_drop" not in captured[0]["tools"]
    # ~6k prompt + ~6k tool result of a 16k window after round 1: over the
    # 45% offer and the 70% ceiling.
    assert len(seen[2][0]) == 1 and "context_drop" in captured[1]["tools"]
    assert len(seen[3][0]) == 1, "the nudge is added once per turn"


def test_no_offer_and_no_nudge_while_the_run_is_short(stores):
    seen = {}

    def script(n, messages):
        seen[n] = _nudges(messages)
        return [("bash", {"command": "make"}, "call_big")] if n == 1 else "Done."

    captured, _e = _drive(stores, script, native=True,
                          settings={"agent_context_tools_offer_round": 12,
                                    "agent_midturn_compact_enabled": False,
                                    "agent_tool_result_offload_chars": 10_000_000})
    assert not seen[2] and "context_drop" not in captured[1]["tools"]


def test_master_switch_off_never_offers(stores):
    def script(n, messages):
        return "Done."
    captured, _e = _drive(stores, script, native=True, user="free up your context",
                          settings={"agent_context_tools_enabled": False})
    assert not ({"context_status", "context_drop"} & set(captured[0]["tools"]))
