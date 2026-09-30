"""Observed current-response usage participates before tool dispatch."""
import json
import pytest
from src import agent_loop as al
from tests.test_main_retry_pending_admission import configure
from tests.test_autonomy_budget import _collect, _events
from tests.test_recovery_budget_receipts import usage

@pytest.mark.parametrize("amount,local,repeat,denied", [
    (1000,False,False,True), (100,False,False,False),
    (1000,True,False,False), (1000,False,True,True),
])
def test_tool_gate_accounts_for_current_main_usage(monkeypatch, amount, local, repeat, denied):
    ledger = configure(monkeypatch)
    calls = []
    effects = []
    async def stream(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            yield usage({"input_tokens": amount, "output_tokens": 0})
            if repeat: yield usage({"input_tokens": amount, "output_tokens": 0})
            yield "data: "+json.dumps({"delta": '```read_file\n{"path":"fixture.txt"}\n```'})+"\n\n"
        else:
            yield 'data: {"delta":"Synthetic completion."}\n\n'
        yield "data: [DONE]\n\n"
    async def execute(block, *a, **kw):
        effects.append(block.tool_type)
        return (block.tool_type, {"output": "QA controlled effect", "exit_code": 0})
    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(al, "execute_tool_block", execute)
    events = _events(_collect(al.stream_agent_loop(
        "http://127.0.0.1:8888/v1" if local else "https://fixture.invalid/v1", "m",
        [{"role":"user", "content":"Read the QA fixture"}], max_rounds=2,
        relevant_tools={"read_file"}, owner="admin")))
    assert effects == ([] if denied else ["read_file"])
    stops = [e for e in events if e.get("type") == "budget_exhausted"]
    assert bool(stops) == denied
    if denied:
        assert len(calls) == 1
        assert stops[0]["kind"] == "tokens" and stops[0]["used"] == amount
        assert stops[0]["checkpoint"]["touched_files"] == []
        assert not any(e.get("type") == "tool_output" for e in events)
