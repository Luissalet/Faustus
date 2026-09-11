"""Lote 70a, puntos A.2 y A.3.

A.2: `src/agent_loop.py`'s round loop already receives `execution_target` on
every bash/python tool result (`subprocess_tools.py`, EXEC-01) but discarded
it before it reached the wire — neither the live `tool_output` SSE event nor
the persisted `tool_event` copied it, even though the Studio side
(`chat.ts::executionTargetFrom`, `Transcript.tsx`) already decodes it.

A.3: `src/tool_approvals.py::PendingToolApproval.public_payload()` showed the
approval card's raw, unmasked command — the one place EXEC-02's
`command_guard.command_preview` (secrets redacted) was not applied — and
never told the client where the command would run.
"""
from __future__ import annotations

import asyncio
import json

from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action


def _collect(gen):
    async def _run():
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


def _events(chunks):
    out = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                out.append(json.loads(chunk[6:]))
            except json.JSONDecodeError:
                continue
    return out


# ── A.2 ──────────────────────────────────────────────────────────────────

def test_tool_output_and_tool_event_carry_execution_target(monkeypatch):
    import src.agent_loop as agent_loop

    target = {"kind": "posix", "cwd": "/work", "shell": "/bin/sh"}

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10, raising=False)
    # `bash` is on the non-admin denylist by default — this test is about
    # the execution_target passthrough, not tool authorization.
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    _round = {"n": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        _round["n"] += 1
        if _round["n"] == 1:
            call = {"name": "bash", "arguments": json.dumps({"command": "echo hi"})}
            yield f'data: {json.dumps({"type": "tool_calls", "calls": [call]})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "The command ran. Anything else?"})}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        return ("bash", {"output": "hi", "exit_code": 0, "execution_target": target})

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream, raising=False)
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute, raising=False)

    chunks = _collect(
        agent_loop.stream_agent_loop(
            "https://api.openai.com/v1", "gpt-4o",
            [{"role": "user", "content": "run echo hi"}],
            relevant_tools={"bash"}, _is_teacher_run=True,
        )
    )
    events = _events(chunks)

    tool_output = next(e for e in events if e.get("type") == "tool_output")
    assert tool_output["execution_target"] == target

    metrics = next(e["data"] for e in events if e.get("type") == "metrics")
    assert metrics["tool_events"][0]["execution_target"] == target


def test_tool_output_omits_execution_target_when_result_has_none(monkeypatch):
    """A tool with no shell-target concept (e.g. one that never sets the
    key) must not gain a spurious `None`/empty entry on the wire."""
    import src.agent_loop as agent_loop

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    _round = {"n": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        _round["n"] += 1
        if _round["n"] == 1:
            call = {"name": "bash", "arguments": json.dumps({"command": "echo hi"})}
            yield f'data: {json.dumps({"type": "tool_calls", "calls": [call]})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "The command ran. Anything else?"})}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        return ("bash", {"output": "hi", "exit_code": 0})

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream, raising=False)
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute, raising=False)

    chunks = _collect(
        agent_loop.stream_agent_loop(
            "https://api.openai.com/v1", "gpt-4o",
            [{"role": "user", "content": "run echo hi"}],
            relevant_tools={"bash"}, _is_teacher_run=True,
        )
    )
    events = _events(chunks)
    tool_output = next(e for e in events if e.get("type") == "tool_output")
    assert "execution_target" not in tool_output


# ── A.3 ──────────────────────────────────────────────────────────────────

def _pending(store, **overrides):
    values = {
        "owner": "Alice",
        "session_id": "session-1",
        "origin_run_id": "run-1",
        "tool_name": "bash",
        "content": "export TOKEN=abcXYZ789secret; printf hi",
        "workspace": None,
        "external_untrusted_context_seen": True,
        "capabilities": capabilities_for_action("bash", "printf hi"),
    }
    values.update(overrides)
    return store.create(**values)


def test_public_payload_masks_secrets_in_command_preview():
    store = ToolApprovalStore()
    pending = _pending(store)
    action = pending.public_payload()["action"]

    # The raw, unmasked command is still shown in `content` (existing
    # contract — the approval must show the COMPLETE sealed input) but the
    # new `command_preview` field is the one a masked-by-default UI reads.
    assert action["content"] == "export TOKEN=abcXYZ789secret; printf hi"
    assert "abcXYZ789secret" not in action["command_preview"]
    assert "printf hi" in action["command_preview"]


def test_public_payload_includes_execution_target_for_shell_tools():
    store = ToolApprovalStore()
    pending = _pending(store, tool_name="bash")
    action = pending.public_payload()["action"]
    assert action["execution_target"]["kind"]

    pending2 = _pending(store, tool_name="python", content="print(1)")
    action2 = pending2.public_payload()["action"]
    assert action2["execution_target"]["kind"]


def test_public_payload_omits_execution_target_for_non_shell_tools():
    store = ToolApprovalStore()
    pending = _pending(store, tool_name="write_file", content="hello")
    action = pending.public_payload()["action"]
    assert "execution_target" not in action
