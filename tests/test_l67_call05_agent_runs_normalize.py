"""Lote 67 — CALL-05 integration: `src.agent_runs._partial_from_events`
now classifies each recovered `tool_output` event with
`src.tool_result.normalize_tool_result(...).status` instead of only
carrying its raw `exit_code` with no interpretation.

The gap this closes: before this lote, `_partial_from_events` copied
`exit_code` into each `tool_events` entry and stopped there — a successful
TRANSPORT (the event reached the replay log) was never told apart from a
successful ACTION. A `blocked` refusal (exit_code often 1, or a policy
sentence with no numeric code at all) and an ordinary non-zero exit both
looked the same as "just some exit_code", and there was no single place
recording that either one is NOT "done". `trace_for_call`/
`recover_interrupted_runs` feed this straight into the recovered assistant
message's `tool_events` metadata the Studio UI renders as tool-call cards —
so this is exactly the "a step marked done/error after a tool call" surface
CALL-05 names, at the one place `src/agent_runs.py` actually reads a raw
tool result dict field by field.
"""
from __future__ import annotations

import json

from src.agent_runs import _partial_from_events


def _tool_output_event(**fields):
    payload = {"type": "tool_output"}
    payload.update(fields)
    return "data: " + json.dumps(payload)


def test_a_clean_exit_normalizes_to_succeeded():
    events = [_tool_output_event(tool="bash", command="ls", output="ok",
                                  exit_code=0, call_id="c1")]
    out = _partial_from_events(events)
    assert out["tool_events"][0]["status"] == "succeeded"


def test_a_transport_successful_but_functionally_failed_call_is_never_succeeded():
    """CALL-05's own acceptance, reproduced directly: the event arrived
    (transport succeeded) but exit_code is non-zero — must not read as
    done."""
    events = [_tool_output_event(tool="bash", command="false", output="",
                                  exit_code=1, call_id="c2")]
    out = _partial_from_events(events)
    entry = out["tool_events"][0]
    assert entry["exit_code"] == 1
    assert entry["status"] == "failed"
    assert entry["status"] != "succeeded"


def test_a_blocked_refusal_is_denied_not_failed_and_not_succeeded():
    """A policy refusal (src.tool_capabilities.blocked_tool_result) is a
    distinct status from an ordinary failure: nothing was attempted."""
    events = [_tool_output_event(tool="edit_file", command="x",
                                  output="blocked: sensitive path",
                                  exit_code=1, blocked=True, call_id="c3")]
    out = _partial_from_events(events)
    entry = out["tool_events"][0]
    assert entry["status"] == "denied"
    assert entry["status"] not in ("succeeded", "failed")


def test_status_is_computed_by_the_shared_classifier_not_reimplemented(monkeypatch):
    """Proves the integration point: agent_runs delegates to
    src.tool_result.normalize_tool_result rather than owning its own
    error/exit_code reading — patching the shared classifier changes what
    _partial_from_events reports."""
    import src.agent_runs as agent_runs
    from src.contracts.tool import ToolResult

    sentinel = ToolResult(call_id="x", attempt_id="x", status="conflict", output={})
    monkeypatch.setattr(agent_runs, "normalize_tool_result", lambda raw: sentinel, raising=False)
    # _partial_from_events imports normalize_tool_result locally inside the
    # loop, so patch it where it is actually looked up: src.tool_result.
    import src.tool_result as tool_result_mod
    monkeypatch.setattr(tool_result_mod, "normalize_tool_result", lambda raw: sentinel)

    events = [_tool_output_event(tool="bash", command="ls", output="ok",
                                  exit_code=0, call_id="c1")]
    out = _partial_from_events(events)
    assert out["tool_events"][0]["status"] == "conflict"


def test_multiple_events_each_get_their_own_independent_status():
    events = [
        _tool_output_event(tool="bash", command="ls", output="ok", exit_code=0, call_id="c1"),
        _tool_output_event(tool="bash", command="false", output="", exit_code=1, call_id="c2"),
        _tool_output_event(tool="edit_file", command="x", output="blocked", exit_code=1,
                            blocked=True, call_id="c3"),
    ]
    out = _partial_from_events(events)
    statuses = [e["status"] for e in out["tool_events"]]
    assert statuses == ["succeeded", "failed", "denied"]
