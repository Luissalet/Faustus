"""A09 (docs/spec/paridad/, Lote T4): deferred tool search returns no match.

Trigger: the model calls `lookup_tools` for something that is not a real
tool. Expect: an explicit, BOUNDED fallback (the N=5 closest tools by name
or capability) — never a fabricated call (a tool that does not exist gets
promoted and then invoked), and never an unbounded schema dump (every tool
this install knows about, handed over so the model can "figure it out
itself").

This drives the REAL `src.agent_loop.stream_agent_loop` with a fake LLM
stream (the only fake, in the harness's own native tool-call event shape —
see `tests/acceptance/test_a08_tool_discovery.py` for the pattern this
reuses verbatim): `execute_tool_block` is patched so `lookup_tools` calls
its REAL handler (`src.agent_tools.interaction_tools.LookupToolsTool` ->
`src.tool_serve.execute_lookup` -> `src.tool_index.get_tool_index()`), the
same code path production uses.
"""

from __future__ import annotations

import asyncio
import json
import unittest.mock as mock

import pytest

import src.agent_tools  # noqa: F401  - resolves the circular schema imports first
import src.agent_loop as agent_loop
import src.tool_security as tool_security
from src.agent_tools.interaction_tools import LookupToolsTool
from src.tool_discovery import FALLBACK_N, is_known_tool

from tests.acceptance.conftest import record_evidence

OLLAMA_V1 = "http://127.0.0.1:11434/v1"
MODEL = "qwen3-coder:30b"

CODE_REQUEST = "Use whatever tool handles interstellar_flux_calibrate for me."

# A name that is not, and will never be, a real Faustus tool.
BOGUS_TOOL = "interstellar_flux_calibrate_xyz"


async def _real_lookup_tools(content: str, ctx: dict):
    return await LookupToolsTool().execute(content, ctx)


def run_turn(message, workspace, *, rounds, lookup_calls, executed):
    script = list(rounds)
    round_box = {"n": 0}

    async def fake_stream(candidates, messages, **kwargs):
        idx = min(round_box["n"], len(script) - 1)
        round_box["n"] += 1
        item = script[idx]
        if isinstance(item, str):
            if item:
                yield "data: " + json.dumps({"delta": item}) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "stop"}) + "\n\n"
        else:
            yield "data: " + json.dumps({"type": "tool_calls", "calls": item}) + "\n\n"
            yield "data: " + json.dumps({"type": "finish", "finish_reason": "tool_calls"}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        executed.append(block.tool_type)
        if block.tool_type == "lookup_tools":
            lookup_calls.append(block.content)
            ctx = {
                "owner": kwargs.get("owner"),
                "disabled_tools": kwargs.get("disabled_tools") or set(),
            }
            return await _real_lookup_tools(block.content, ctx)
        return (block.tool_type, {"output": "ok", "exit_code": 0})

    patches = [
        mock.patch.object(agent_loop, "stream_llm_with_fallback", fake_stream),
        mock.patch.object(agent_loop, "execute_tool_block", fake_execute),
        mock.patch.object(agent_loop, "get_mcp_manager", lambda: None),
        mock.patch.object(agent_loop, "estimate_tokens", lambda *a, **k: 10),
        mock.patch.object(agent_loop, "get_setting", lambda k, d=None: d),
        mock.patch.object(agent_loop, "blocked_tools_for_owner", lambda o: set()),
        mock.patch.object(tool_security, "owner_is_admin_or_single_user", lambda o: True),
    ]
    for patch in patches:
        patch.start()
    try:
        async def drive():
            stream = agent_loop.stream_agent_loop(
                endpoint_url=OLLAMA_V1,
                model=MODEL,
                messages=[{"role": "user", "content": message}],
                headers={},
                workspace=workspace,
                owner="admin",
                session_id="s-a09",
                max_rounds=len(script) + 1,
                context_length=32768,
                relevant_tools={"read_file", "ls", "lookup_tools"},
                disabled_tools=set(),
                harness_options={"checkpoints": False, "run_tests": False, "repo_map": False},
            )
            return [chunk async for chunk in stream]
        chunks = asyncio.run(drive())
    finally:
        for patch in patches:
            patch.stop()

    assert round_box["n"], "the loop never reached the provider"
    events = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                events.append(json.loads(chunk[6:]))
            except json.JSONDecodeError:
                pass
    return events


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "cart.py").write_text("def total(items):\n    return 0\n")
    return str(tmp_path)


@pytest.mark.acceptance("A09")
def test_no_match_gives_bounded_fallback_never_a_fabricated_call(workspace, request):
    assert not is_known_tool(BOGUS_TOOL), "the fixture picked a real tool by accident"

    lookup_calls: list = []
    executed: list = []
    rounds = [
        [{"name": "lookup_tools", "arguments": json.dumps({"names": [BOGUS_TOOL]})}],
        # The model gives up rather than invent a call — this is what the
        # bounded fallback in the tool result is supposed to make natural.
        "I don't have a tool for that.",
    ]

    events = run_turn(CODE_REQUEST, workspace, rounds=rounds, lookup_calls=lookup_calls, executed=executed)

    assert lookup_calls, "lookup_tools was never invoked"

    # (1) No fabricated call: the bogus name was never dispatched as a real
    # tool execution, by ANY name.
    assert BOGUS_TOOL not in executed
    assert executed == ["lookup_tools"]

    # (2) The `tool_discovery` audit records an explicit, BOUNDED fallback —
    # never empty-handed, never the whole catalog.
    discovery_events = [e for e in events if e.get("type") == "tool_discovery"]
    assert discovery_events, "no tool_discovery audit event was emitted"
    audit = discovery_events[0]
    assert audit["resolved"] == []
    assert BOGUS_TOOL not in audit["resolved"]
    assert 0 < len(audit["fallback"]) <= FALLBACK_N
    assert "no such tool" in audit["reason"] or "no match" in audit["reason"]
    assert "invented" in audit["reason"] or "no such tool" in audit["reason"]

    # (3) Every fallback name is a REAL tool — never a guess dressed up as a
    # resolved one.
    for name in audit["fallback"]:
        assert is_known_tool(name), name

    # (4) Bounded payload: the raw `lookup_tools` tool_output the model saw
    # is small, not "every schema dumped to compensate". A full schema dump
    # of this install's ~130 built-in tools runs tens of KB; the fallback
    # catalog entry is name+one-liner only, capped at N=5.
    lookup_outputs = [e for e in events if e.get("type") == "tool_output" and e.get("tool") == "lookup_tools"]
    assert lookup_outputs
    raw_output = lookup_outputs[0].get("output") or ""
    assert len(raw_output) < 4000, f"lookup_tools output was {len(raw_output)} bytes — looks like a schema dump"

    record_evidence(
        request,
        mechanism="src/tool_discovery.py::audit_selection (unknown-tool branch) + nearest_by_name_or_capability",
        sse_event="tool_discovery",
        fallback=audit["fallback"],
        fallback_size=len(audit["fallback"]),
    )
