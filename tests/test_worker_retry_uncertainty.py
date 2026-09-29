"""Delegate's real one()/worker path: uncertain outcomes never auto-retry."""
import asyncio
import json

import pytest
from src import agent_loop, agent_runs
from src.agent_tools import subagent_tools as st
from tests.test_subagent_causal_identity import history


@pytest.fixture
def delegate(history, monkeypatch):
    monkeypatch.setattr(agent_runs, "mark_busy", lambda *a, **k: None)
    monkeypatch.setattr(agent_runs, "clear_busy", lambda *a, **k: None)
    settings = {"agent_subagent_max_parallel": 1, "agent_subagent_supervisor": False,
                "agent_subagent_tick_seconds": 0.05}
    monkeypatch.setattr(st, "_setting", lambda key, default=None: settings.get(key, default))
    st._SLOTS.clear()
    return history


async def execute():
    return await st.DelegateAgentsTool().execute(json.dumps({"tasks": [{"name": "fixture",
        "instruction": "Read the fixture and implement synthetic app.py, then verify it."}],
        "parallel": False, "reviewer": False, "timeout_s": 30}),
        {"session_id": "parent", "owner": None, "progress_cb": None})


def source(monkeypatch, outcome, *, text="Done.", legacy=False):
    invocations = []
    async def stream(*args, **kwargs):
        invocations.append(kwargs["harness_options"]["run_id"])
        yield 'data: {"type":"tool_output","tool":"read_file","call_id":"read-native","result_status":"succeeded","exit_code":0,"output":"Synthetic source."}\n\n'
        event = {"type": "tool_output", "tool": "bash", "call_id": "uncertain-native",
                 "exit_code": None, "output": "Synthetic reported result."}
        event["status" if legacy else "result_status"] = outcome
        if outcome == "failed":
            event["exit_code"] = 1
        yield "data: " + json.dumps(event) + "\n\n"
        yield "data: " + json.dumps({"delta": text}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
    return invocations


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["outcome_unknown", "partial", "cancelled"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_uncertain_plus_successful_read_blocks_second_attempt(delegate, monkeypatch, outcome, legacy):
    invocations = source(monkeypatch, outcome, legacy=legacy)
    result = await execute()
    report = result["subagents"][0]
    assert report["receipts"][0]["verdict"] == "ack_only"
    assert report["tool_calls"] == 2 and report["failed_calls"] == 1
    assert len(invocations) == 1  # !_all_refused: this fence is the new cause
    assert report["retry_blocked_uncertainty"]
    child = delegate.get_session(report["session_id"])
    stored = child.history[-1].metadata
    assert stored["retry_blocked_uncertainty"]
    assert stored["tool_events"][-1]["call_id"] == "uncertain-native"
    assert stored["tool_events"][-1]["result_status"] == outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "denied", "conflict"])
async def test_known_failure_keeps_retry_and_new_uuid(delegate, monkeypatch, outcome):
    invocations = source(monkeypatch, outcome)
    report = (await execute())["subagents"][0]
    assert len(invocations) == 2 and invocations[0] != invocations[1]
    assert not report.get("retry_blocked_uncertainty")
    assert len(report["receipts"]) == 2


@pytest.mark.asyncio
async def test_clean_ack_only_still_retries(delegate, monkeypatch):
    ids = []
    async def stream(*args, **kwargs):
        ids.append(kwargs["harness_options"]["run_id"])
        yield 'data: {"delta":"Done."}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
    report = (await execute())["subagents"][0]
    assert len(ids) == 2 and ids[0] != ids[1]
    assert not report.get("retry_blocked_uncertainty")


@pytest.mark.asyncio
async def test_unknown_signal_not_overridden_by_prose(delegate, monkeypatch):
    invocations = source(monkeypatch, "outcome_unknown", text="Everything definitely succeeded.")
    report = (await execute())["subagents"][0]
    assert report["receipts"][0]["verdict"] == "empty"
    assert len(invocations) == 1 and report["retry_blocked_uncertainty"]


@pytest.mark.asyncio
async def test_malformed_canonical_outcome_uses_normalized_unknown(delegate, monkeypatch):
    invocations = source(monkeypatch, ["invalid legacy result"])
    report = (await execute())["subagents"][0]
    assert len(invocations) == 1 and report["retry_blocked_uncertainty"]
