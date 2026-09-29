"""H04: a response is not proof that an external effect completed or rolled back."""
import asyncio
import json

import pytest

from src import agent_runs, tool_execution
from src.agent_tools import ToolBlock
from src.contracts.tool import ToolResult
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT
from src.tool_result import normalize_tool_result


@pytest.mark.parametrize("status", ["succeeded", "failed", "partial", "cancelled", "denied", "conflict", "outcome_unknown"])
def test_declared_contract_status_survives_normalization(status):
    raw = {"status": status, "output": "detail"}
    typed = normalize_tool_result(raw, call_id="call_1", attempt_id="attempt_1")
    assert ToolResult.from_mapping(typed.to_mapping()).status == status
    assert typed.call_id == "call_1" and typed.attempt_id == "attempt_1"
    assert raw == {"status": status, "output": "detail"}


def test_unknown_preserves_producer_reconciliation_advice():
    raw = {"status": "outcome_unknown", "uncertainty": {
        "reason": "receipt timed out", "reconcile_action": "look up delivery id 42"}}
    assert normalize_tool_result(raw).uncertainty.to_mapping() == raw["uncertainty"]


def test_explicit_timeout_uncertainty_wins_over_contradictory_blocked_flag():
    result = normalize_tool_result({"timed_out": True, "blocked": True})
    assert result.status == "outcome_unknown"
    assert result.uncertainty.reconcile_action == "read_current_state_before_retry"


@pytest.mark.parametrize("state", ["partial", "unknown"])
def test_recovery_does_not_discard_uncertain_effect_on_unrecognized_event(state):
    def event(value):
        return "data: " + json.dumps({"type": "tool_effect", "call_id": "c", "state": value}) + "\n\n"
    partial = agent_runs._partial_from_events([event("pending"), event(state), event("future-status")])
    assert partial["unknown_effects"][0]["state"] == state
    assert agent_runs._partial_from_events([event(state), event("confirmed")])["unknown_effects"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [
    ("partial", "partial"), ("outcome_unknown", "unknown"),
    ("timed_out", "unknown"), ("cancelled", "unknown"),
    ("denied", "failed"), ("failed", "failed"),
])
async def test_effect_result_is_persisted_and_survives_recovery_without_redispatch(tmp_path, monkeypatch, status, expected):
    from tests.acceptance.test_a05_unknown_effect import _Sess, _SM, SESSION_ID
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    monkeypatch.setattr(agent_runs, "_INTERRUPTED", {})
    dispatches = []
    ready = asyncio.Event()

    async def handler(*args, **kwargs):
        dispatches.append("dispatched")
        if status not in {"denied", "failed"}:
            (tmp_path / "external-effect.txt").write_text("effect already happened")
        return "synthetic external effect", {"status": status}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", handler)

    async def turn():
        await tool_execution.execute_tool_block(
            ToolBlock("write_file", '{}'), session_id=SESSION_ID, owner="alice",
            call_id="effect-1", security_context=NO_TOOL_SECURITY_CONTEXT)
        ready.set()
        await asyncio.Event().wait()
        yield "data: [DONE]\n\n"

    run = agent_runs.start(SESSION_ID, turn())
    try:
        await asyncio.wait_for(ready.wait(), 5)
        lines = open(run.log.path, encoding="utf-8").read().splitlines()
        events = [json.loads(line)["ev"] for line in lines if "ev" in json.loads(line)]
        effects = [json.loads(ev[6:]) for ev in events if ev.startswith("data: {") and json.loads(ev[6:]).get("type") == "tool_effect"]
        assert [effect["state"] for effect in effects] == ["pending", expected]
        assert effects[-1]["result_status"] == ("outcome_unknown" if status == "timed_out" else status)
        if expected == "unknown" and status != "cancelled":
            assert effects[-1]["uncertainty"]["reconcile_action"]
    finally:
        run.task.cancel()
        try:
            await run.task
        except asyncio.CancelledError:
            pass
        agent_runs._RUNS.clear()
    # Keep the log captured before cancellation, exactly the disk state at a crash.
    with open(run.log.path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    sess = _Sess()
    recovered = agent_runs.recover_interrupted_runs(_SM(sess))
    assert len(recovered) == 1
    unknown = recovered[0].get("unknown_effects", [])
    if expected in {"unknown", "partial"}:
        assert unknown[0]["state"] == expected
        assert (tmp_path / "external-effect.txt").exists()
        assert "Do not repeat" in agent_runs.unknown_effects_system_block(sess)
    else:
        assert unknown == []
    assert dispatches == ["dispatched"]
