"""Lot I wiring — see `I_wiring.md` at the worktree root for the exact diffs.

Three integrator-file touch points were investigated for this lot:

1. `src/tool_execution.py` dispatch — reading the full dispatch chain shows
   `manage_instincts` (registered in `TOOL_HANDLERS` by this branch) already
   falls through to the existing generic `elif tool in dynamic_handlers`
   fallback with NO change needed. `test_manage_instincts_dispatches_via_generic_tool_handlers`
   below asserts that directly and is NOT xfail — it passes today.
2. `src/agent_loop.py` — needs the `render_block` injection diff in
   `I_wiring.md` §2. `test_agent_loop_injects_instincts_block` is
   `xfail(strict=True)` until that lands.
3. `routes/chat_helpers.py` — needs the post-turn extraction diff in
   `I_wiring.md` §3. `test_chat_helpers_fires_instinct_extraction` is
   `xfail(strict=True)` until that lands.
"""
from __future__ import annotations

import asyncio
import json
from collections import namedtuple
from types import SimpleNamespace

import pytest

ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])


# ── 1. tool_execution.py dispatch: already works, no xfail ────────────────

def test_manage_instincts_dispatches_via_generic_tool_handlers(tmp_path, monkeypatch):
    import src.instincts as instincts_mod
    monkeypatch.setattr(instincts_mod, "DATA_DIR", str(tmp_path), raising=False)

    from src.tool_capabilities import ToolRunSecurityContext
    from src.tool_execution import execute_tool_block

    async def _run():
        context = ToolRunSecurityContext()
        return await execute_tool_block(
            ToolBlock("manage_instincts", json.dumps({"action": "status"})),
            owner="alice", security_context=context,
        )

    desc, result = asyncio.run(_run())
    assert "manage_instincts" in desc
    assert isinstance(result, dict)
    # do_manage_instincts wraps every payload under "results", same
    # convention as do_manage_skills (see src/tools/system.py).
    assert result.get("results", {}).get("total") == 0  # fresh owner, no instincts yet


def test_manage_instincts_is_in_tool_handlers_with_a_generic_adapter():
    from src.agent_tools import TOOL_HANDLERS
    assert "manage_instincts" in TOOL_HANDLERS
    import inspect
    sig = inspect.signature(TOOL_HANDLERS["manage_instincts"])
    assert list(sig.parameters) == ["content", "ctx"]


# ── 2. agent_loop.py injection (I_wiring.md §2) ────────────────────────────

def test_agent_loop_injects_instincts_block(tmp_path, monkeypatch):
    import src.instincts as instincts_mod
    monkeypatch.setattr(instincts_mod, "DATA_DIR", str(tmp_path), raising=False)
    instincts_mod.upsert("alice", {
        "trigger": "when writing new fastapi routes",
        "action": "use the router factory in routes/ and register in app.py",
        "domain": "workflow", "scope": "project", "project": "/repo",
        "confidence": 0.9,
    })

    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    seen_messages = []

    async def fake_stream(candidates, messages, **kwargs):
        seen_messages.extend(messages)
        yield "data: " + json.dumps({"delta": "Done."}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)

    async def _collect():
        chunks = []
        async for chunk in agent_loop.stream_agent_loop(
            "http://local.test/v1", "small-local-model",
            [{"role": "user", "content": "add a new endpoint for invoices"}],
            owner="alice", workspace="/repo", max_rounds=1,
            relevant_tools=set(),
        ):
            chunks.append(chunk)
        return chunks

    asyncio.run(_collect())

    joined = json.dumps(seen_messages)
    assert "Learned instincts" in joined
    assert "router factory" in joined


# ── 3. chat_helpers.py post-turn extraction (I_wiring.md §3) ──────────────

class _FakeSessionWithHistory:
    def __init__(self, owner="alice"):
        self.model = "selected-model"
        self.owner = owner
        self.name = "Chat"
        self.history = []

    def add_message(self, message):
        self.history.append(message)

    def get_context_messages(self):
        return [{"role": m.role, "content": m.content} for m in self.history]


def test_chat_helpers_fires_instinct_extraction(tmp_path, monkeypatch):
    import routes.chat_helpers as chat_helpers
    import src.instincts as instincts_mod

    monkeypatch.setattr(instincts_mod, "DATA_DIR", str(tmp_path), raising=False)

    calls = []

    async def _fake_extract(owner, session_id, messages, *, project, project_name, workspace):
        calls.append({"owner": owner, "session_id": session_id})
        return []

    monkeypatch.setattr(instincts_mod, "extract_from_turn", _fake_extract)
    monkeypatch.setattr(
        "services.projects.project_context_for_session",
        lambda session_id, owner=None: SimpleNamespace(
            project_id="", project_name="", owner=owner, workspace="", session_id=session_id, source="none",
        ),
    )

    sess = _FakeSessionWithHistory()
    fake_session_manager = SimpleNamespace(save_sessions=lambda: None)

    chat_helpers.save_assistant_response(
        sess, session_manager=fake_session_manager, session_id="s1",
        full_response="Here is the fix.",
        last_metrics={"model": "actual-model"},
        tool_events=[{"round": 1, "tool": "bash", "command": "x", "output": "ok", "exit_code": 0},
                     {"round": 2, "tool": "bash", "command": "y", "output": "ok", "exit_code": 0}],
    )

    # The hook schedules extraction with asyncio.ensure_future — give the
    # event loop one pass to run it.
    async def _drain():
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_drain())
    # Outside a running loop the hook falls back to a daemon thread; give it
    # a moment before judging.
    import time as _time
    for _ in range(40):
        if calls:
            break
        _time.sleep(0.05)

    assert calls, "extract_from_turn was never called for a 2-tool-call turn"
    assert calls[0]["owner"] == "alice"
    assert calls[0]["session_id"] == "s1"
