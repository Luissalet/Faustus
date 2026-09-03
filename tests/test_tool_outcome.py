"""Four-value outcomes (src/tool_outcome.py) and the places that used to call
everything that did not finish a failure.

    success | expected_error | cancelled | panic

The one that matters in practice: a worker the USER stopped is `cancelled`, not
a failed worker — not in its own run record, not in the dispatched job's
`totals.errors`, not in the per-model scorecard. A worker that really broke
still is. `agent_tool_outcomes` off = the old arithmetic exactly.
"""
from __future__ import annotations

import json

import pytest

from src import dispatch, scorecard
from src.tool_outcome import Outcome, classify_result, classify_status, counts_as_failure, value_of


@pytest.fixture
def outcomes(monkeypatch):
    """Toggle `agent_tool_outcomes` (and read the other settings for real)."""
    import src.settings as settings_mod
    values = {}
    real = settings_mod.get_setting
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: values[key] if key in values else real(key, default))
    return values


# ── the classifier ──────────────────────────────────────────────────────────

def test_a_plain_successful_result_is_success():
    assert classify_result({"output": "hi", "exit_code": 0}) is Outcome.SUCCESS
    assert classify_result({"output": "hi"}) is Outcome.SUCCESS
    assert classify_result("a tool that returned text") is Outcome.SUCCESS
    assert classify_result(None) is Outcome.SUCCESS


def test_a_refusal_is_an_expected_error_not_a_panic():
    """A tool that correctly says no: the policy gate, the approval gate, the
    command guard, the sub-agent file lock."""
    assert classify_result({"blocked": True, "error": "blocked by policy"}) is Outcome.EXPECTED_ERROR
    assert classify_result({"approval_required": True}) is Outcome.EXPECTED_ERROR
    assert classify_result({"error": "bash: this command needs your approval", "exit_code": 2}) is Outcome.EXPECTED_ERROR
    assert classify_result({"error": "edit_file: 'a.py' is owned by sub-agent 'w1'", "exit_code": 1}) is Outcome.EXPECTED_ERROR
    # an ordinary non-zero exit is also expected: the tool ran and failed
    assert classify_result({"error": "command not found", "exit_code": 127}) is Outcome.EXPECTED_ERROR


def test_an_unhandled_exception_is_a_panic():
    assert classify_result({"error": "ValueError: bad json", "exit_code": 1}) is Outcome.PANIC
    assert classify_result({"error": "Traceback (most recent call last):\n  File ..."}) is Outcome.PANIC
    assert classify_result({"error": "internal error", "exit_code": 1}) is Outcome.PANIC
    assert classify_status("error", error="TypeError: x") is Outcome.PANIC


def test_cancelled_wins_over_everything():
    assert classify_result({"output": "ok", "exit_code": 0}, cancelled=True) is Outcome.CANCELLED
    assert classify_result({"error": "boom", "exit_code": 1}, cancelled=True) is Outcome.CANCELLED
    assert classify_result({"cancelled": True, "error": "stopped"}) is Outcome.CANCELLED
    assert classify_result({"status": "stopped"}) is Outcome.CANCELLED
    assert classify_status("stopped") is Outcome.CANCELLED
    assert classify_status("interrupted") is Outcome.CANCELLED


def test_worker_stop_reasons_map_the_way_the_reports_read_them():
    assert classify_status("complete") is Outcome.SUCCESS
    assert classify_status("done") is Outcome.SUCCESS
    assert classify_status("timeout") is Outcome.EXPECTED_ERROR
    assert classify_status("stalled") is Outcome.EXPECTED_ERROR
    assert classify_status("rounds_exhausted") is Outcome.EXPECTED_ERROR
    assert classify_status("stopped") is Outcome.CANCELLED


def test_only_the_two_error_outcomes_count_as_failures():
    assert counts_as_failure(Outcome.EXPECTED_ERROR) and counts_as_failure("panic")
    assert not counts_as_failure(Outcome.CANCELLED) and not counts_as_failure("success")
    assert not counts_as_failure("nonsense") and not counts_as_failure(None)


def test_outcomes_serialise_as_their_plain_string():
    assert json.dumps({"o": Outcome.CANCELLED}) == '{"o": "cancelled"}'
    assert value_of("cancelled") == "cancelled" and value_of("nope") is None


def test_the_classifier_never_raises_on_junk():
    for junk in (object(), 42, [1, 2], {"exit_code": "not a number"}, {"exit_code": None}):
        assert isinstance(classify_result(junk), Outcome)
    assert isinstance(classify_status(None), Outcome)


# ── a stopped worker is not a failed worker ─────────────────────────────────

def _report(**over):
    base = {"id": "sa1", "name": "w", "session_id": "c", "status": "done", "stop_reason": "complete",
            "error": None, "tool_calls": 2, "failed_calls": 0, "mutations": [], "rounds": 1,
            "input_tokens": 1, "output_tokens": 1, "final_text": "", "role": "worker"}
    base.update(over)
    return base


def test_a_cancelled_worker_is_not_counted_as_an_error(outcomes):
    outcomes["agent_tool_outcomes"] = True
    result = {"subagents": [
        _report(name="stopped", status="stopped", stop_reason="stopped"),
        _report(name="ok"),
    ]}
    c = dispatch.compact_from_result(result)
    assert c["totals"]["errors"] == 0
    assert c["totals"]["cancelled"] == 1
    assert [w["outcome"] for w in c["workers"]] == ["cancelled", "success"]


def test_a_worker_that_really_broke_still_counts(outcomes):
    outcomes["agent_tool_outcomes"] = True
    result = {"subagents": [
        _report(name="crashed", status="error", stop_reason="error", error="RuntimeError: exploded"),
        _report(name="timed out", status="timeout", stop_reason="timeout"),
        _report(name="stopped", status="stopped", stop_reason="stopped"),
    ]}
    c = dispatch.compact_from_result(result)
    assert c["totals"]["errors"] == 2 and c["totals"]["cancelled"] == 1
    assert [w["outcome"] for w in c["workers"]] == ["panic", "expected_error", "cancelled"]


def test_with_the_setting_off_a_stopped_worker_counts_as_an_error_again(outcomes):
    """The invariant: off = the arithmetic that shipped, and no new field."""
    outcomes["agent_tool_outcomes"] = False
    result = {"subagents": [_report(name="stopped", status="stopped", stop_reason="stopped"), _report(name="ok")]}
    c = dispatch.compact_from_result(result)
    assert c["totals"] == {"tool_calls": 4, "failed_calls": 0, "rounds": 2, "input_tokens": 2,
                           "output_tokens": 2, "errors": 1}
    assert "cancelled" not in c["totals"]
    assert all("outcome" not in w for w in c["workers"])


def test_the_worker_report_carries_its_own_outcome(outcomes):
    from src.agent_tools import subagent_tools as st
    outcomes["agent_tool_outcomes"] = True
    run = st.SubagentRun(0, {"name": "w", "instruction": "do x"})
    assert run.outcome() == "success" or run.stop_reason == "unknown"
    run.stop_reason = "stopped"
    run.stopped_by_user = True
    assert run.outcome() == "cancelled" and run.report()["outcome"] == "cancelled"
    assert run.report()["status"] == "stopped"          # the old field is untouched
    run.stop_reason, run.stopped_by_user, run.error = "error", False, "OSError: no such file"
    assert run.report()["outcome"] == "panic"
    outcomes["agent_tool_outcomes"] = False
    assert run.outcome() is None and "outcome" not in run.report()


def test_a_stopped_run_record_is_cancelled(outcomes):
    from src import agent_runs
    outcomes["agent_tool_outcomes"] = True
    run = agent_runs._Run(label="a chat")
    assert run.outcome is None               # still running: no outcome yet
    run.status = "done"
    assert run.outcome == "success"
    run.status = "stopped"
    assert run.outcome == "cancelled"
    run.status = "error"
    assert run.outcome == "panic"
    outcomes["agent_tool_outcomes"] = False
    assert run.outcome is None


async def test_stopping_one_worker_end_to_end_lands_as_cancelled(outcomes, tmp_path, monkeypatch):
    """The Stop button path, for real: `stop_worker` cancels one worker while
    the coordinator finishes the rest. The stopped one reports `cancelled` and
    the job's error tally stays at zero."""
    import asyncio
    import src.agent_loop as al
    import src.ai_interaction as ai
    from src import tool_execution as te
    from src.agent_tools import subagent_tools as st

    outcomes["agent_tool_outcomes"] = True
    monkeypatch.setattr(te, "get_active_workspace", lambda: str(tmp_path))
    monkeypatch.setattr(te, "get_active_workspace_roots", lambda: ())

    class _SM:
        def __init__(self):
            self.sessions = {"parent": type("P", (), {
                "endpoint_url": "http://127.0.0.1:11434/v1", "model": "m", "headers": None, "name": "p"})()}

        def create_session(self, session_id, **kw):
            self.sessions[session_id] = type("S", (), {
                "messages": [], "add_message": lambda self, m: self.messages.append(m)})()

        def get_session(self, sid):
            return self.sessions.get(sid)

        def save_sessions(self):
            pass

    monkeypatch.setattr(ai, "get_session_manager", lambda: _SM())
    slow_running = asyncio.Event()

    async def _loop(endpoint_url, model, messages, **kwargs):
        if "slow task" in messages[0]["content"]:
            slow_running.set()
            await asyncio.sleep(30)
        yield "data: " + json.dumps({"type": "harness_summary",
                                     "data": {"mutations": [], "stop_reason": "complete"}}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_agent_loop", _loop)
    task = asyncio.create_task(st.DelegateAgentsTool().execute(json.dumps({
        "tasks": [{"name": "slow", "instruction": "slow task"}, {"name": "fast", "instruction": "fast task"}],
        "parallel": False, "timeout_s": 60,
    }), {"session_id": "parent", "owner": None, "progress_cb": None}))
    await asyncio.wait_for(slow_running.wait(), 5)
    for _ in range(100):
        await asyncio.sleep(0.02)
        if st.active_worker_ids():
            break
    assert st.stop_worker(st.active_worker_ids()[0]) is True
    result = await asyncio.wait_for(task, 5)

    reports = {r["name"]: r for r in result["subagents"]}
    assert reports["slow"]["stop_reason"] == "stopped" and reports["slow"]["outcome"] == "cancelled"
    assert reports["fast"]["outcome"] == "success"
    totals = dispatch.compact_from_result(result)["totals"]
    assert totals["errors"] == 0 and totals["cancelled"] == 1


def test_the_scorecard_records_the_outcome_and_counts_stops_apart(outcomes):
    outcomes["agent_tool_outcomes"] = True
    done = scorecard.build_entry(session_id="s1", model="m", endpoint_label="local", workspace="/w",
                                 user_text="fix it", duration_s=10, rounds=3,
                                 harness={"stop_reason": "complete", "mutations": ["a.py"], "notes": []})
    stopped = scorecard.build_entry(session_id="s2", model="m", endpoint_label="local", workspace="/w",
                                    user_text="fix it", duration_s=2, rounds=1,
                                    harness={"stop_reason": "stopped", "mutations": []})
    assert done["outcome"] == "success" and stopped["outcome"] == "cancelled"
    row = scorecard.aggregate([done, stopped])[0]
    assert row["cancelled"] == 1
    # the stop never entered the verified rate — that is over FINISHED turns
    assert row["verified_rate"] == 100.0 and row["turns"] == 2
    # a row written before the field existed reads the same way
    assert scorecard.entry_outcome({"stop_reason": "stopped"}) == "cancelled"
    outcomes["agent_tool_outcomes"] = False
    assert "outcome" not in scorecard.build_entry(
        session_id="s3", model="m", endpoint_label="local", workspace="/w", user_text="x",
        duration_s=1, rounds=1, harness={"stop_reason": "stopped", "mutations": []})
