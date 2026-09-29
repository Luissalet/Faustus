import asyncio
import json

import pytest

from src import agent_loop, agent_runs, ai_interaction, budget_account
from src.agent_tools import subagent_tools as st


@pytest.fixture
def attempts(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: None)
    monkeypatch.setattr(agent_runs, "mark_busy", lambda *a: None)
    monkeypatch.setattr(agent_runs, "clear_busy", lambda *a: None)
    monkeypatch.setattr(budget_account, "default_path", lambda: tmp_path / "budget.db")
    run = st.SubagentRun(0, {"name": "worker", "instruction": "Write report"})
    reports = []

    async def emit(event):
        reports.append(event)

    async def execute(events, failure=None):
        async def stream(*a, **kw):
            for event in events:
                yield "data: " + json.dumps(event) + "\n\n"
            if failure is not None:
                raise failure

        monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
        await st._run_subagent(run, endpoint_url="http://unused", model="test",
            headers=None, owner=None, workspace=None, workspace_roots=None,
            max_rounds=2, shared_context="", parent_session_id=None,
            emit=emit, save_transcript=False)

    return run, reports, execute


def rounds(input_tokens, output_tokens):
    return {"type": "round_info", "round": 1,
            "input_tokens": input_tokens, "output_tokens": output_tokens}


def metrics(input_tokens, output_tokens):
    return {"type": "metrics", "data": {"input_tokens": input_tokens, "output_tokens": output_tokens}}


@pytest.mark.asyncio
async def test_retry_totals_preserve_prior_attempt_and_reconcile_real_sqlite(attempts):
    run, reports, execute = attempts
    budget_account.open("run", ceiling_tokens=1000)
    budget_account.reserve("run", run.id, 500)
    await execute([rounds(90, 15), metrics(100, 20)])
    await execute([rounds(40, 5), metrics(50, 10), metrics(50, 10), rounds(50, 10)])
    assert (run.input_tokens, run.output_tokens) == (150, 30)
    assert reports[-1]["input_tokens"] == 150
    assert reports[-1]["output_tokens"] == 30
    budget_account.reconcile("run", run.id, run.input_tokens + run.output_tokens)
    snapshot = budget_account.snapshot("run")
    assert snapshot["consumed_tokens"] == 180
    assert snapshot["reserved_tokens"] == 0
    assert snapshot["remaining_tokens"] == 820
    assert snapshot["consumed_cost"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError("stream interrupted"), asyncio.CancelledError()])
async def test_retry_without_metrics_keeps_observed_usage_on_failure_or_cancel(attempts, failure):
    run, _, execute = attempts
    await execute([rounds(100, 20), metrics(100, 20)])
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await execute([rounds(30, 5), rounds(20, 5)], failure)
    else:
        await execute([rounds(30, 5), rounds(20, 5)], failure)
    assert (run.input_tokens, run.output_tokens) == (150, 30)


@pytest.mark.asyncio
async def test_three_attempts_mix_estimates_final_totals_and_zero_usage(attempts):
    run, _, execute = attempts
    await execute([rounds(100, 20)])
    await execute([rounds(40, 5), metrics(50, 10)])
    await execute([metrics(0, 0)])
    assert (run.input_tokens, run.output_tokens) == (150, 30)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
async def test_partial_metrics_do_not_erase_unreported_dimension(attempts, field):
    run, _, execute = attempts
    await execute([metrics(100, 20)])
    await execute([rounds(40, 5), {"type": "metrics", "data": {field: 50}}, rounds(10, 2)])
    expected = (150, 27) if field == "input_tokens" else (150, 70)
    assert (run.input_tokens, run.output_tokens) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, -1, float("nan"), float("inf"), float("-inf"), True, "20"])
async def test_invalid_metrics_preserve_observed_usage(attempts, invalid):
    run, _, execute = attempts
    await execute([metrics(100, 20)])
    await execute([rounds(40, 5), metrics(invalid, invalid), rounds(10, 5)])
    assert (run.input_tokens, run.output_tokens) == (150, 30)
    assert run.error is None or not run.error


@pytest.mark.asyncio
async def test_invalid_round_usage_does_not_reduce_or_poison_totals(attempts):
    run, _, execute = attempts
    await execute([metrics(100, 20)])
    await execute([rounds(-10, float("nan")), rounds(float("inf"), True), rounds(50, 10)])
    assert (run.input_tokens, run.output_tokens) == (150, 30)
