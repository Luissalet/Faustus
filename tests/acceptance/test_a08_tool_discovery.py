"""A08 (docs/spec/paridad/, Lote T4): necessary tool omitted from the initial
prompt.

Trigger: the model needs a tool that was never put in this turn's native
schema list (the tool-RAG selection omitted it, or the caller scoped
`relevant_tools` narrowly). Expect: discovery (`lookup_tools`, the existing
"third path" in `src/tool_serve.py`) loads the EXACT permitted schema for
that tool — not just "not in disabled_tools" but the same predicate real
execution uses (`tool_policy`/the non-admin denylist too, via
`src.tool_discovery.is_permitted`) — and the selection + resolution is
recorded as an auditable event, not just a debug log line.

This drives the REAL `src.agent_loop.stream_agent_loop` with a fake LLM
stream (the only fake, scripted the same way
`tests/test_agent_harness_loop.py::_native_call_stream` does: the harness's
own native function-calling event, `{"type": "tool_calls", "calls":
[{"name","arguments"}]}`, parsed at `src/agent_loop.py`'s
`native_tool_calls = data.get("calls", [])`): `execute_tool_block` is
patched so `lookup_tools` calls its REAL handler
(`src.agent_tools.interaction_tools.LookupToolsTool` ->
`src.tool_serve.execute_lookup` -> `src.tool_index.get_tool_index()`),
exactly the code path production uses; every other tool call in this test is
a trivial canned success, since the mechanism under test is discovery, not
those other tools.
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
from src.tool_policy import ToolPolicy

from tests.acceptance.conftest import record_evidence

OLLAMA_V1 = "http://127.0.0.1:11434/v1"
MODEL = "qwen3-coder:30b"

CODE_REQUEST = (
    "In this workspace, find where `total` is defined before changing "
    "anything."
)

# `find_symbol` is a real, natively-schema'd tool (see
# src/tool_index.py::BUILTIN_TOOL_DESCRIPTIONS / src/tool_schemas.py) that is
# NOT part of the default set a bound-workspace turn is handed (unlike
# read_file/grep/glob/edit_file/bash, which the workspace floor restores
# regardless of `relevant_tools`) and is not preflighted off for a no-project
# turn either (unlike `project_context`/`manage_project_context`/...) — so it
# is a genuine "omitted from the initial prompt" case, not one the floor or
# the preflight would explain away on their own.
TARGET_TOOL = "find_symbol"


async def _real_lookup_tools(content: str, ctx: dict):
    """The real production handler for `lookup_tools` — not a fake."""
    return await LookupToolsTool().execute(content, ctx)


def run_turn(
    message,
    workspace,
    *,
    relevant_tools,
    disabled_tools=None,
    tool_policy=None,
    lookup_calls,
    executed,
    owner="admin",
    rounds,
):
    """Drive the real `stream_agent_loop`. Only the provider stream and the
    non-`lookup_tools` tool executions are faked; `lookup_tools` runs for
    real. `rounds` is a list of round scripts, one per round, each either a
    plain string (text reply, `finish_reason="stop"`) or a list of native
    tool calls `[{"name", "arguments"}]` (`finish_reason="tool_calls"`). The
    last entry repeats once the script runs out.

    `executed` collects every tool_type the harness actually dispatched, in
    order — the ground truth for "was the promoted tool really callable",
    since this environment resolves to prompt-style tool listing (no
    `tools=` kwarg to the provider) rather than the API function-calling
    route, so the schema's presence has to be read off what got executed,
    not off the (empty here) `tools` kwarg.

    Returns the decoded SSE events.
    """
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
        # Single-user/admin: `lookup_tools`'s own admin check AND the
        # in-loop A08/A09 wiring (`src.tool_discovery.is_permitted`) both
        # resolve the owner through this same function.
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
                owner=owner,
                session_id="s-a08",
                max_rounds=len(script) + 1,
                context_length=32768,
                relevant_tools=set(relevant_tools),
                disabled_tools=set(disabled_tools or ()),
                tool_policy=tool_policy,
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
    (tmp_path / "cart.py").write_text(
        "def total(items):\n    return sum(i['price'] for i in items)\n"
    )
    return str(tmp_path)


@pytest.mark.acceptance("A08")
def test_discovery_loads_exact_permitted_schema_and_is_audited(workspace, request):
    # Deliberately OMITTED from the initial selection — the trigger
    # ("necessary tool omitted from initial prompt").
    relevant_tools = {"read_file", "ls", "lookup_tools"}
    lookup_calls: list = []
    executed: list = []

    rounds = [
        # Round 1: the model discovers what it does not have (native call).
        [{"name": "lookup_tools", "arguments": json.dumps({"names": [TARGET_TOOL]})}],
        # Round 2: the exact schema is now loaded — the model calls it.
        [{"name": TARGET_TOOL, "arguments": json.dumps({"name": "total"})}],
        # Round 3: done.
        "Found it.",
    ]

    events = run_turn(
        CODE_REQUEST, workspace,
        relevant_tools=relevant_tools,
        lookup_calls=lookup_calls,
        executed=executed,
        rounds=rounds,
    )

    # (1) The trigger condition — the tool was never in the initial
    # selection, so the model had to ask for it before it could run.
    assert TARGET_TOOL not in relevant_tools

    # (2) The model actually called the real `lookup_tools` handler (not a
    # stub) with the omitted tool's name.
    assert lookup_calls, "lookup_tools was never invoked"
    assert json.loads(lookup_calls[0]).get("names") == [TARGET_TOOL]

    # (3) Discovery loaded the EXACT permitted schema for the next round —
    # the harness accepted and dispatched the SECOND call to the (until now
    # unknown) tool as a genuine, real call, not a hallucination it had to
    # correct (a `harness_check`/`unknown_tool` event, produced when a call
    # names something the round's schema list never carried, never fires).
    assert executed == ["lookup_tools", TARGET_TOOL]
    unknown_tool_events = [
        e for e in events
        if e.get("type") == "harness_check" and e.get("status") == "unknown_tool"
    ]
    assert not unknown_tool_events, unknown_tool_events
    tool_outputs = [e for e in events if e.get("type") == "tool_output" and e.get("tool") == TARGET_TOOL]
    assert tool_outputs and tool_outputs[0].get("exit_code") == 0

    # (4) Audit: selection + resolution recorded as a structured event, not
    # just a log line — `tool_discovery` (docs/api/sse_events.json).
    discovery_events = [e for e in events if e.get("type") == "tool_discovery"]
    assert discovery_events, "no tool_discovery audit event was emitted"
    audit = discovery_events[0]
    assert audit["query"] == "" and audit["candidates"] == [TARGET_TOOL]
    assert audit["resolved"] == [TARGET_TOOL]
    assert "permitted" in audit["reason"]

    record_evidence(
        request,
        mechanism="src/tool_discovery.py::audit_selection + agent_loop.py lookup_tools promotion",
        sse_event="tool_discovery",
        resolved_tool=TARGET_TOOL,
        executed=executed,
    )


@pytest.mark.acceptance("A08")
def test_discovery_never_promotes_a_tool_policy_denied_tool(workspace, request):
    """The "exact PERMITTED schema" half of A08, end to end: a tool this
    turn's `tool_policy` denies must never be promoted, whatever the model
    asks `lookup_tools` for.

    `agent_loop.py` folds `tool_policy.all_disabled_names()` into
    `disabled_tools` once, before either surface is built (the same
    reconciliation `tests/test_agent_loop_offer_execute_coherence.py`
    pins), so `src/tool_serve.py`'s own disabled_tools-only filter already
    keeps a policy-denied name out of its `tools`/`promote` list — it never
    even surfaces as a raw "candidate". `src.tool_discovery.audit_selection`
    still runs afterwards and correctly reports nothing to resolve; see
    `test_is_permitted_rejects_a_tool_policy_denial_on_its_own` below for the
    predicate exercised directly against `tool_policy`, independent of that
    merge.
    """
    relevant_tools = {"read_file", "ls", "lookup_tools"}
    lookup_calls: list = []
    executed: list = []
    denying_policy = ToolPolicy(disabled_tools=frozenset({TARGET_TOOL}))

    rounds = [
        [{"name": "lookup_tools", "arguments": json.dumps({"names": [TARGET_TOOL]})}],
        f"{TARGET_TOOL} is not available to me right now.",
    ]

    events = run_turn(
        CODE_REQUEST, workspace,
        relevant_tools=relevant_tools,
        tool_policy=denying_policy,
        lookup_calls=lookup_calls,
        executed=executed,
        rounds=rounds,
    )

    assert lookup_calls, "lookup_tools was never invoked"
    # The tool was never actually dispatched — nothing promoted it, and the
    # model (scripted to give up after round 1) never tried.
    assert TARGET_TOOL not in executed

    discovery_events = [e for e in events if e.get("type") == "tool_discovery"]
    assert discovery_events, "no tool_discovery audit event was emitted"
    audit = discovery_events[0]
    assert audit["resolved"] == []
    assert TARGET_TOOL not in audit["candidates"]
    # A09's bounded fallback still applies here — nothing was invented.
    assert len(audit["fallback"]) <= 5

    record_evidence(
        request,
        mechanism="agent_loop.py tool_policy/disabled_tools reconciliation + src/tool_discovery.py::audit_selection",
        sse_event="tool_discovery",
        candidates=audit["candidates"],
        resolved=audit["resolved"],
    )


def test_is_permitted_rejects_a_tool_policy_denial_on_its_own():
    """`src.tool_discovery.is_permitted` — the predicate `agent_loop.py`'s
    `lookup_tools` promotion step calls — must refuse a `tool_policy` denial
    by itself, independent of whether some OTHER layer already folded that
    denial into `disabled_tools` first (see the acceptance test above for
    the case where it did). This is the defense-in-depth half of "exact
    PERMITTED schema": a caller that hands discovery a `tool_policy` without
    also pre-merging it into `disabled_tools` must still get refused, not a
    promoted schema it would then be denied for."""
    from src.tool_discovery import is_permitted, audit_selection

    policy = ToolPolicy(disabled_tools=frozenset({TARGET_TOOL}))
    assert is_permitted(TARGET_TOOL, disabled_tools=set(), tool_policy=policy, admin=True) is False
    assert is_permitted("read_file", disabled_tools=set(), tool_policy=policy, admin=True) is True

    audit = audit_selection(
        "", [TARGET_TOOL], disabled_tools=set(), tool_policy=policy, admin=True,
    )
    assert audit.candidates == [TARGET_TOOL]
    assert audit.resolved == []
    assert "denylist" in audit.reason
