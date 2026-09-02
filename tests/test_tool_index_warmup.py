"""Startup warmup default and the agent loop's index-init timeout.

The tool index warmup used to be gated behind ODYSSEUS_STARTUP_WARMUPS (off by
default), so the first agent turn always paid the index build and, with the
per-request selection timeout, landed on always-available tools only. Now the
tool index warms by default (its own switch to disable it), and an index init
that does exceed the timeout falls through to keyword selection like the
retrieval timeout already did.
"""

import json
import os
import time


# ── startup warmup default ─────────────────────────────────────────────────

def test_tool_index_warmup_is_on_by_default_and_can_be_disabled():
    from src.tool_index import tool_index_warmup_enabled

    assert tool_index_warmup_enabled(env={}) is True
    assert tool_index_warmup_enabled(env={"ODYSSEUS_TOOL_INDEX_WARMUP": "0"}) is False
    assert tool_index_warmup_enabled(env={"ODYSSEUS_TOOL_INDEX_WARMUP": "false"}) is False
    assert tool_index_warmup_enabled(env={"ODYSSEUS_TOOL_INDEX_WARMUP": "1"}) is True
    # an explicit opt-out of all startup warmups still covers the tool index …
    assert tool_index_warmup_enabled(env={"ODYSSEUS_STARTUP_WARMUPS": "0"}) is False
    # … unless the tool-index switch says otherwise
    assert tool_index_warmup_enabled(env={"ODYSSEUS_STARTUP_WARMUPS": "0", "ODYSSEUS_TOOL_INDEX_WARMUP": "1"}) is True
    # the legacy flag being merely unset/empty does not turn it off
    assert tool_index_warmup_enabled(env={"ODYSSEUS_STARTUP_WARMUPS": ""}) is True


def test_app_startup_gates_tool_index_warmup_on_the_helper():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "app.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "tool_index_warmup_enabled" in src
    # the endpoint pings stay opt-in
    assert "ODYSSEUS_STARTUP_WARMUPS" in src


# ── agent loop: a slow index build must not strip the tools the query named ──

def _sent_tool_names(monkeypatch, message, *, slow_index_seconds):
    import asyncio
    import src.agent_loop as al
    import src.tool_index as ti

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(al, "_TOOL_SELECTION_TIMEOUT_SECONDS", 0.05, raising=False)

    def _slow_get_tool_index():
        time.sleep(slow_index_seconds)
        return None

    monkeypatch.setattr(ti, "get_tool_index", _slow_get_tool_index, raising=False)

    captured = []

    async def _fake_stream(_candidates, messages, **kwargs):
        captured.append(kwargs.get("tools"))
        yield "data: " + json.dumps({"delta": "ok"}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    async def _run():
        gen = al.stream_agent_loop(
            "https://api.openai.com/v1", "gpt-test",
            [{"role": "user", "content": message}],
            max_rounds=1, relevant_tools=None, owner="admin",
        )
        return [c async for c in gen]

    asyncio.run(_run())
    schemas = captured[0] or []
    return {t["function"]["name"] for t in schemas if isinstance(t, dict) and "function" in t}


def test_index_init_timeout_falls_back_to_keyword_selection(monkeypatch):
    # "latest news" makes the turn a real (web) request; "second opinion" is a
    # keyword hint (chat_with_model) that no domain seed covers, so it reaches
    # the model only through the keyword fallback.
    names = _sent_tool_names(
        monkeypatch, "check the latest news and ask gpt for a second opinion", slow_index_seconds=0.5,
    )
    assert "chat_with_model" in names
    assert "web_search" in names
    assert "ask_user" in names
