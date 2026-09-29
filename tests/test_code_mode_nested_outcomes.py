import asyncio

import pytest

from src.code_mode import bridge, runner
from src.code_mode.outcomes import MAX_RECEIPTS
from src.agent_harness import TurnLedger


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(runner, "_limits", lambda: {"timeout_seconds": 3,
        "max_calls": 100, "max_output_bytes": 20000, "max_memory_bytes": 0})


@pytest.mark.asyncio
@pytest.mark.parametrize("nested,outer", [("outcome_unknown", "outcome_unknown"),
    ("cancelled", "outcome_unknown"), ("partial", "partial"), ("succeeded", None)])
async def test_nested_status_reaches_harness_without_changing_guest_transport(monkeypatch, nested, outer):
    async def dispatch(*a, **kw):
        return {"status": nested, "exit_code": 0, "output": "private-body",
                "uncertainty": {"reason": "r" * 800, "reconcile_action": "check", "body": "excluded"}}
    monkeypatch.setattr(bridge, "dispatch_call", dispatch)
    result = await runner.run_code_mode("r=tools.call('send_email', {'body':'private-argument'}); print(r['status'])")
    assert result["exit_code"] == 0
    assert result.get("status") == outer
    assert nested in result["output"]
    receipt = result["tool_outcomes"]
    assert receipt["counts"][nested] == 1
    assert "private" not in str(receipt)
    assert len(receipt["records"][0].get("uncertainty", {}).get("reason", "")) <= 512
    ledger = TurnLedger()
    ledger.record("run_code", "script", result)
    assert ledger.record_progress([{"content": "Complete task", "status": "completed"}], 1)[0]["verified"] is (outer is None)


@pytest.mark.asyncio
async def test_known_error_handled_by_script_does_not_fail_execution(monkeypatch):
    async def dispatch(*a, **kw):
        return {"status": "failed", "error": "expected failure", "exit_code": 1}
    monkeypatch.setattr(bridge, "dispatch_call", dispatch)
    result = await runner.run_code_mode("r=tools.call('read_file', {}); assert r['error']; print('handled')")
    assert result["exit_code"] == 0
    assert "status" not in result
    assert result["tool_outcomes"]["counts"]["failed"] == 1


@pytest.mark.asyncio
async def test_bounded_records_keep_exact_counts_and_do_not_reconcile_unknown(monkeypatch):
    count = 0
    async def dispatch(*a, **kw):
        nonlocal count
        count += 1
        return {"status": "outcome_unknown" if count == MAX_RECEIPTS + 1 else "succeeded"}
    monkeypatch.setattr(bridge, "dispatch_call", dispatch)
    result = await runner.run_code_mode(f"for i in range({MAX_RECEIPTS + 3}): tools.call('read_file', {{}})")
    receipt = result["tool_outcomes"]
    assert receipt["calls"] == MAX_RECEIPTS + 3
    assert sum(receipt["counts"].values()) == receipt["calls"]
    assert receipt["counts"]["outcome_unknown"] == 1
    assert len(receipt["records"]) == MAX_RECEIPTS
    assert receipt["omitted_records"] == 3
    assert result["status"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_timeout_during_dispatch_preserves_inflight_receipt(monkeypatch):
    monkeypatch.setattr(runner, "_limits", lambda: {"timeout_seconds": 1,
        "max_calls": 10, "max_output_bytes": 20000, "max_memory_bytes": 0})
    async def dispatch(*a, **kw):
        await asyncio.sleep(10)
    monkeypatch.setattr(bridge, "dispatch_call", dispatch)
    result = await runner.run_code_mode("tools.call('send_email', {})")
    assert result["exit_code"] == 1
    assert result["receipt"]["terminated_by"] == "timeout"
    assert result["status"] == "outcome_unknown"
    assert result["tool_outcomes"]["counts"]["outcome_unknown"] == 1
    assert result["tool_outcomes"]["records"][0]["call_id"].startswith("code_mode:")
