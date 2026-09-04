"""A refused worker tool call, carried from the loop to the coordinator.

The gap this covers, reproduced live: a read-only `reviewer` was dispatched
with an instruction that demanded a write. It called `write_file`, was
blocked, said out loud it would try `edit_file` instead, was blocked again,
and the file on disk never changed. The enforcement was right. What the
coordinator was handed was

    worker revisor2: status=done tools=3 failed=2

which cannot tell a permission refusal from a crashed tool, so the only
sensible next move it left was a retry that could only be refused again.

`ToolDenial` (src/agent_loop.py) already knew every part of the answer. These
tests are about carrying it: onto the run, into the compact projection, and
onto the verdict line — without turning a refusal into a failure, and without
costing an ordinary worker a single byte.
"""
from __future__ import annotations

import json

import pytest

from src import dispatch
from src.agent_tools import subagent_tools as st
from src.subagent_permissions import ChildPermissions

from tests.test_subagent_board_events import delegation, _delegate, _ev, _harness_summary  # noqa: F401


DENYLIST = "request_denylist"


def _reviewer(name: str = "revisor2") -> st.SubagentRun:
    """A read-only worker, exactly as `agents/reviewer.md` produces one: an
    allowlist, so everything else is the deny of its complement."""
    run = st.SubagentRun(0, {"name": name, "instruction": "corrige el bug", "agent": "reviewer"})
    run.permissions = ChildPermissions(slug="reviewer", allowed_tools=frozenset({"read_file", "ls", "grep"}))
    return run


def _deny(run: st.SubagentRun, tool: str, *, times: int = 1, origin: str = DENYLIST) -> None:
    for _ in range(times):
        run.note_refusal(tool, policy=origin, origin=origin, matched=tool)


# ── K1: the denials land on the run, grouped and bounded ──────────────────

def test_a_worker_denied_twice_reports_one_refusal_that_names_the_definition():
    run = _reviewer()
    _deny(run, "edit_file", times=2)
    assert run.report()["refusals"] == [
        {"tool": "edit_file", "policy": DENYLIST, "count": 2,
         "why": "denied by agent definition 'reviewer'"},
    ]


def test_forty_denials_of_the_same_tool_are_one_entry_with_the_count():
    """The report is read by the thing that decides whether to retry, so it
    must not grow with the retrying."""
    run = _reviewer()
    _deny(run, "edit_file", times=40)
    refusals = run.report()["refusals"]
    assert len(refusals) == 1 and refusals[0]["count"] == 40


def test_the_group_bound_holds_for_many_different_tools():
    run = _reviewer()
    for i in range(st.MAX_REFUSAL_GROUPS + 15):
        _deny(run, f"tool_{i}")
    assert len(run.refusals) == st.MAX_REFUSAL_GROUPS


def test_two_tools_from_two_layers_keep_two_different_causes():
    """The failure this whole mechanism exists to prevent, one layer down.

    A worker's denylist is composed from three unrelated decisions and reaches
    the loop as one flat set, so both of these come back as `request_denylist`.
    Reporting them with the same word is the line that cost twenty minutes.
    """
    run = st.SubagentRun(0, {"name": "w1", "instruction": "haz algo"})  # no definition
    _deny(run, "create_session")   # the hard set: no sub-agent ever gets this
    _deny(run, "web_search")       # the lean guess: this task did not ask for it
    causes = {x["tool"]: x["why"] for x in run.report()["refusals"]}
    assert causes["create_session"] == "not available to sub-agents"
    assert causes["web_search"] == "not in this worker's lean toolset"
    assert len(set(causes.values())) == 2


def test_a_denial_from_somewhere_else_keeps_its_raw_origin():
    """An unfamiliar word sends its reader to the source; a confident wrong
    one sends it nowhere. Plan mode is not the worker's own denylist."""
    run = _reviewer()
    _deny(run, "bash", origin="plan_mode_readonly")
    assert run.report()["refusals"][0]["why"] == "denied by plan_mode_readonly"


def test_the_hard_set_outranks_the_definition_because_that_is_the_order():
    """`worker_disabled_tools` applies the hard set unconditionally and the
    definition after it, so a hard-set tool is not the author's doing."""
    run = _reviewer()
    _deny(run, "create_session")
    assert run.report()["refusals"][0]["why"] == "not available to sub-agents"


def test_a_worker_with_no_definition_still_says_a_definition_denied_nothing():
    run = st.SubagentRun(0, {"name": "w1", "instruction": "x"})
    run.permissions = ChildPermissions(denied_tools=frozenset({"bash"}))
    _deny(run, "bash")
    assert run.report()["refusals"][0]["why"] == "denied by this worker's agent definition"


# ── every existing reader of `rejections` still works ─────────────────────

def test_rejections_stays_the_int_its_readers_expect():
    """`rejections` counts the harness rejecting a round's claims. A refusal
    is a different cause and gets a different field: one word for two causes
    is the bug this branch exists to stop, not one to repeat."""
    run = _reviewer()
    run.rejections = 2
    _deny(run, "edit_file")
    report = run.report()
    assert report["rejections"] == 2 and isinstance(report["rejections"], int)
    assert report["refusals"][0]["tool"] == "edit_file"


def test_the_prose_report_still_prints_harness_rejections_and_now_the_refusals():
    run = _reviewer()
    run.rejections = 3
    run.stop_reason = "complete"
    run.finished = run.started + 1
    _deny(run, "edit_file", times=2)
    text = st._build_report_text([run], None)
    assert "harness rejections: 3" in text
    assert "edit_file ×2 — denied by agent definition 'reviewer'" in text
    assert "do not re-ask" in text


def test_a_worker_that_was_refused_nothing_reports_no_refusals_key():
    run = _reviewer()
    run.rejections = 1
    assert "refusals" not in run.report()


# ── the wiring: a blocked tool_output becomes a refusal on the run ────────

@pytest.mark.asyncio
async def test_a_blocked_tool_output_is_recorded_as_a_refusal(delegation):  # noqa: F811
    """End to end over the stream the loop really emits for a denial."""
    def _blocked(tool):
        return _ev({"type": "tool_output", "tool": tool, "command": "{}", "exit_code": 1,
                    "output": f"Tool '{tool}' is disabled by the current request policy.",
                    "blocked": True, "policy": "disabled_tools",
                    "policy_name": DENYLIST, "policy_origin": DENYLIST, "policy_matched": tool})

    async def _loop(endpoint_url, model, messages, **kwargs):
        yield _blocked("write_file")
        yield _blocked("edit_file")
        yield _blocked("edit_file")
        yield _harness_summary([])
        yield "data: [DONE]\n\n"

    delegation(_loop)
    result = await _delegate([{"name": "revisor2", "instruction": "corrige el bug"}], [], parallel=False)
    report = result["subagents"][0]
    # The counters are unchanged — a refusal really is a failed call — and the
    # report can now say which of the failures they were.
    assert report["tool_calls"] == 3 and report["failed_calls"] == 3
    by_tool = {x["tool"]: x for x in report["refusals"]}
    assert by_tool["edit_file"]["count"] == 2 and by_tool["write_file"]["count"] == 1


@pytest.mark.asyncio
async def test_an_ordinary_failed_tool_call_is_not_a_refusal(delegation):  # noqa: F811
    async def _loop(endpoint_url, model, messages, **kwargs):
        yield _ev({"type": "tool_output", "tool": "bash", "command": "pytest", "exit_code": 1,
                   "output": "1 failed"})
        yield _harness_summary([])
        yield "data: [DONE]\n\n"

    delegation(_loop)
    result = await _delegate([{"name": "w1", "instruction": "run the tests"}], [], parallel=False)
    report = result["subagents"][0]
    assert report["failed_calls"] == 1 and "refusals" not in report


# ── K2: the compact projection ────────────────────────────────────────────

_PLAIN_WORKER = {
    "id": 0, "name": "w1", "session_id": "child-1", "status": "done", "stop_reason": "complete",
    "error": None, "tool_calls": 7, "failed_calls": 1, "rounds": 4, "rejections": 0,
    "mutations": ["cart.py"], "input_tokens": 12000, "output_tokens": 900, "duration_s": 41.2,
    "model": "qwen3.5:9b", "role": "worker", "files": [], "instruction": "add apply_tax",
    "final_text": "done", "started_at": 1.0, "ended_at": 42.2, "steered": 0, "supervisor": [],
    "outcome": "success",
}

#: The row a coordinator has been reading. Frozen on purpose: `refusals` is a
#: new key on a projection whose size is a documented win (FAUSTUS.md §21), so
#: "only when there are any" has to be a test and not an intention.
_FROZEN_ROW = {
    "name": "w1", "role": "worker", "status": "done", "stop_reason": "complete", "error": None,
    "rounds": 4, "tool_calls": 7, "failed_calls": 1, "files_changed": ["cart.py"],
    "input_tokens": 12000, "output_tokens": 900, "duration_s": 41.2, "model": "qwen3.5:9b",
    "summary": "done", "session_id": "child-1", "outcome": "success",
}


def test_a_worker_with_no_refusals_has_the_row_it_always_had(monkeypatch):
    monkeypatch.setattr(dispatch, "_outcomes_on", lambda: True)
    row = dispatch.compact_from_result({"subagents": [_PLAIN_WORKER]})["workers"][0]
    assert json.dumps(row, sort_keys=True) == json.dumps(_FROZEN_ROW, sort_keys=True)


def test_refusals_add_exactly_one_key_and_change_nothing_else(monkeypatch):
    monkeypatch.setattr(dispatch, "_outcomes_on", lambda: True)
    refused = dict(_PLAIN_WORKER, refusals=[
        {"tool": "edit_file", "policy": DENYLIST, "count": 2,
         "why": "denied by agent definition 'reviewer'"}])
    row = dispatch.compact_from_result({"subagents": [refused]})["workers"][0]
    assert row.pop("refusals") == [
        {"tool": "edit_file", "why": "denied by agent definition 'reviewer'", "count": 2}]
    assert json.dumps(row, sort_keys=True) == json.dumps(_FROZEN_ROW, sort_keys=True)


def test_the_compact_refusal_is_a_name_a_cause_and_a_count_and_not_the_prose():
    """It earns its bytes by stopping the next re-dispatch, not by explaining
    itself: the sentence handed to the model stays out of the projection."""
    refused = dict(_PLAIN_WORKER, refusals=[{
        "tool": "edit_file", "policy": DENYLIST, "count": 2,
        "why": "denied by agent definition 'reviewer'",
        "reason": "Tool 'edit_file' is disabled by the current request policy " + "x" * 4000,
    }])
    row = dispatch.compact_from_result({"subagents": [refused]})["workers"][0]
    assert set(row["refusals"][0]) == {"tool", "why", "count"}
    assert len(json.dumps(row["refusals"])) < 200


def test_a_flood_of_refusal_groups_is_bounded_in_the_projection_too():
    refused = dict(_PLAIN_WORKER, refusals=[
        {"tool": f"t{i}", "policy": DENYLIST, "count": 1, "why": "denied by " + "x" * 500}
        for i in range(50)])
    row = dispatch.compact_from_result({"subagents": [refused]})["workers"][0]
    assert len(row["refusals"]) == 10
    assert all(len(x["why"]) <= 120 for x in row["refusals"])


# ── K3: the verdict ───────────────────────────────────────────────────────

def _settled(workers) -> str:
    job = dispatch.DispatchJob("luis", {"tasks": []}, "/ws", "", "m", None, "t")
    job.result = {"subagents": workers, "exit_code": 0}
    dispatch._settle(job)
    return job.verdict or ""


def test_the_verdict_names_a_refusal_without_calling_it_a_failure():
    verdict = _settled([dict(_PLAIN_WORKER, name="revisor2", mutations=[], refusals=[
        {"tool": "edit_file", "policy": DENYLIST, "count": 2,
         "why": "denied by agent definition 'reviewer'"}])])
    assert "refused, not failed" in verdict
    assert "revisor2 → edit_file ×2" in verdict
    assert "denied by agent definition 'reviewer'" in verdict
    # The worker finished. It is not an error and the counts must not say it is.
    assert verdict.startswith("1/1 workers done")


def test_the_verdict_is_unchanged_when_nothing_was_refused():
    assert "refused" not in _settled([_PLAIN_WORKER])


def test_a_worker_stopped_by_its_own_limits_is_still_a_done_worker():
    """The distinction the verdict could not draw: changed nothing because it
    was not allowed to, versus changed nothing because there was nothing to
    do. Both are `done`; only one of them says why."""
    idle = _settled([dict(_PLAIN_WORKER, mutations=[])])
    refused = _settled([dict(_PLAIN_WORKER, mutations=[], refusals=[
        {"tool": "write_file", "policy": DENYLIST, "count": 1, "why": "denied by agent definition 'reviewer'"}])])
    assert idle != refused and "refused" in refused
