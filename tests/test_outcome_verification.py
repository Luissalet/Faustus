"""H16: one verification step classifies work as verified / unverified / uncertain.

The single function (src/outcome_verification.py) is fed by evidence the repo
already produces: tool results with exit codes, test runs, process states,
change-set proofs, Code Mode receipts and worker results. Every path that used
to decide "did it work" on its own now goes through it: the harness ledger and
its completion check, the completion gate, Code Mode results, dispatch jobs and
external-worker results.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src import outcome_verification as ov
from src.outcome_verification import UNCERTAIN, UNVERIFIED, VERIFIED, verify_outcome


def kinds(v, bucket="reasons"):
    return [row["kind"] for row in v[bucket]]


HOST = {"mode": "host_process", "filesystem_isolated": False, "network_isolated": False,
        "tool_policy_scope": "tools.call_only"}


def confined_guarantees(scope="workspace_read_only", network_isolated=True):
    return {"mode": "container", "filesystem_isolated": True, "filesystem_scope": scope,
            "network_isolated": network_isolated, "tool_policy_scope": "tools.call_only",
            "host_environment_passed": False, "secrets_passed": False}


# ── the function itself ────────────────────────────────────────────────────

def test_no_evidence_is_never_verified():
    v = verify_outcome()
    assert v["outcome"] == UNVERIFIED and kinds(v) == ["no_evidence"]
    assert verify_outcome(tool_results=[{"tool": "read_file"}])["outcome"] == UNVERIFIED, \
        "a result with no exit code, flag or status confirms nothing"


def test_verified_needs_a_positive_observation_and_carries_it():
    v = verify_outcome(tool_results=[{"tool": "bash", "exit_code": 0, "ok": True}])
    assert v["outcome"] == VERIFIED and kinds(v, "positive") == ["tool_succeeded"] and not v["reasons"]
    assert v["sources"] == ["tool_results"] and v["schema_version"] == 1


def test_exit_code_failure_is_unverified_even_beside_a_success():
    v = verify_outcome(tool_results=[{"tool": "bash", "exit_code": 2, "ok": False, "error": "boom"},
                                     {"tool": "edit_file", "ok": True}])
    assert v["outcome"] == UNVERIFIED and kinds(v) == ["tool_failed"]
    assert "boom" in v["reasons"][0]["detail"]


def test_a_failure_is_cancelled_out_only_by_a_later_success_of_the_same_tool():
    retried = verify_outcome(tool_results=[{"tool": "bash", "exit_code": 1, "ok": False},
                                           {"tool": "bash", "exit_code": 0, "ok": True}])
    assert retried["outcome"] == VERIFIED and "failure_recovered" in kinds(retried, "notes")
    other = verify_outcome(tool_results=[{"tool": "bash", "exit_code": 1, "ok": False},
                                         {"tool": "edit_file", "exit_code": 0, "ok": True}])
    assert other["outcome"] == UNVERIFIED
    lookup = verify_outcome(tool_results=[{"tool": "grep", "ok": False, "kind": "read"},
                                          {"tool": "edit_file", "ok": True}])
    assert lookup["outcome"] == VERIFIED and "lookup_failed" in kinds(lookup, "notes"), \
        "an empty lookup is not a failure of the work"


@pytest.mark.parametrize("row,kind", [
    ({"tool": "bash", "status": "outcome_unknown"}, "result_outcome_unknown"),
    ({"tool": "bash", "result_status": "cancelled", "exit_code": 0}, "result_cancelled"),
    ({"tool": "bash", "status": "partial"}, "result_partial"),
])
def test_unconfirmed_results_are_uncertain_not_failed_and_not_verified(row, kind):
    v = verify_outcome(tool_results=[row, {"tool": "x", "ok": True, "exit_code": 0}])
    assert v["outcome"] == UNCERTAIN and kind in kinds(v)


def test_blocked_and_approval_held_calls_did_not_run():
    assert kinds(verify_outcome(tool_results=[{"tool": "bash", "approval_required": True}])) == ["waiting_approval"]
    assert kinds(verify_outcome(tool_results=[{"tool": "bash", "blocked": True, "error": "no"}])) == ["blocked"]


def test_unverified_beats_uncertain_beats_verified():
    both = verify_outcome(tool_results=[{"tool": "a", "exit_code": 1, "ok": False},
                                        {"tool": "b", "status": "outcome_unknown"},
                                        {"tool": "c", "ok": True}])
    assert both["outcome"] == UNVERIFIED and kinds(both) == ["tool_failed", "result_outcome_unknown"]
    doubt = verify_outcome(tool_results=[{"tool": "b", "status": "outcome_unknown"}, {"tool": "c", "ok": True}])
    assert doubt["outcome"] == UNCERTAIN


def test_tests_evidence_decides():
    assert verify_outcome(tests={"ran": True, "ok": True, "command": "pytest"})["outcome"] == VERIFIED
    failed = verify_outcome(tests={"ran": True, "ok": False, "summary": "2 failed"},
                            tool_results=[{"tool": "edit_file", "ok": True}])
    assert failed["outcome"] == UNVERIFIED and kinds(failed) == ["tests_failed"]
    assert verify_outcome(tests={"ran": True, "ok": False, "inconclusive": True})["outcome"] == UNCERTAIN
    assert verify_outcome(tests={"ran": True, "ok": False, "pre_existing_only": True})["outcome"] == UNCERTAIN
    assert verify_outcome(tests={"ran": False, "ok": None})["outcome"] == UNVERIFIED, "a run that did not happen proves nothing"
    assert verify_outcome(ui_smoke={"ran": True, "ok": False})["outcome"] == UNVERIFIED
    assert verify_outcome(ui_smoke={"ran": True, "ok": True})["outcome"] == VERIFIED


def test_completion_check_reasons_are_unverified_claims():
    bad = verify_outcome(tool_results=[{"tool": "read_file", "ok": True}],
                         completion={"ok": False, "reasons": ["claims_without_mutation"]})
    assert bad["outcome"] == UNVERIFIED and kinds(bad) == ["claim_unsupported"]
    stall = verify_outcome(tool_results=[{"tool": "read_file", "ok": True}],
                           completion={"ok": False, "reasons": ["asked_instead_of_continuing"]})
    assert kinds(stall) == ["stopped_short"]
    clean = verify_outcome(tool_results=[{"tool": "edit_file", "ok": True}], completion={"ok": True, "reasons": []})
    assert clean["outcome"] == VERIFIED


def test_changeset_verdicts():
    assert verify_outcome(changeset={"verdict": "proved"})["outcome"] == VERIFIED
    assert verify_outcome(changeset={"verdict": "contradicted"})["outcome"] == UNVERIFIED
    assert verify_outcome(changeset={"verdict": "unproved", "unsupported_claims": [{"path": "a.py"}]})["outcome"] == UNVERIFIED
    assert verify_outcome(changeset={"verdict": "unproved"})["outcome"] == UNVERIFIED     # nothing positive
    assert verify_outcome(changeset={"verdict": "partial"})["outcome"] == UNCERTAIN


# ── processes ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rec,outcome,kind", [
    ({"handle": "p1", "state": "exited", "exit_code": 0}, VERIFIED, "process_exited_ok"),
    ({"handle": "p1", "state": "exited", "exit_code": 3}, UNVERIFIED, "process_exited_nonzero"),
    ({"handle": "p1", "state": "exited"}, UNCERTAIN, "process_exit_unknown"),
    ({"handle": "p1", "state": "failed_to_start", "termination_reason": "launch_failed"}, UNVERIFIED, "process_failed_to_start"),
    ({"handle": "p1", "state": "lost", "termination_reason": "server restarted"}, UNCERTAIN, "process_lost"),
    ({"handle": "p1", "state": "uncertain"}, UNCERTAIN, "process_uncertain"),
    ({"handle": "p1", "state": "orphaned"}, UNCERTAIN, "process_orphaned"),
    ({"handle": "p1", "state": "timed_out"}, UNCERTAIN, "process_timed_out"),
])
def test_process_states(rec, outcome, kind):
    v = verify_outcome(processes=[rec])
    assert v["outcome"] == outcome and (kind in kinds(v) or kind in kinds(v, "positive"))


def test_a_running_or_stopped_process_is_neither_proof_nor_failure():
    for state in ("running", "starting", "stopped"):
        v = verify_outcome(processes=[{"handle": "p", "state": state}])
        assert v["outcome"] == UNVERIFIED and kinds(v) == ["no_evidence"]
        assert kinds(v, "notes")[0].startswith("process_")


# ── Code Mode ──────────────────────────────────────────────────────────────

def cm(guarantees, **extra):
    return {"exit_code": 0, "runtime_guarantees": guarantees, "calls_made": 0, **extra}


def test_host_direct_effects_are_always_uncertain():
    v = verify_outcome(code_mode=[cm(HOST)])
    assert v["outcome"] == UNCERTAIN and "code_mode_direct_effects_unobserved" in kinds(v)
    assert "code_mode_exit_0" in kinds(v, "positive"), "the run succeeded; its effects are what is unknown"
    # not even a proved change diff, and not a clean nested-call tally, resolves it
    with_diff = verify_outcome(code_mode=[cm(HOST, tool_outcomes={"calls": 1, "counts": {"succeeded": 1}})],
                               changeset={"verdict": "proved"})
    assert with_diff["outcome"] == UNCERTAIN
    assert verify_outcome(code_mode=[{"exit_code": 0}])["outcome"] == UNCERTAIN, "a result with no guarantees is treated as host"


def test_confined_read_only_no_network_can_be_verified():
    v = verify_outcome(code_mode=[cm(confined_guarantees())])
    assert v["outcome"] == VERIFIED and not v["reasons"]


def test_confined_write_and_network_grants_leave_direct_effects_open():
    rw = verify_outcome(code_mode=[cm(confined_guarantees("workspace_read_write"))])
    assert rw["outcome"] == UNCERTAIN and "code_mode_direct_effects_unobserved" in kinds(rw)
    # a change diff the checkpoint actually saw covers workspace writes (and only those)
    covered = verify_outcome(code_mode=[cm(confined_guarantees("workspace_read_write"))],
                             changeset={"verdict": "proved"})
    assert covered["outcome"] == VERIFIED and "code_mode_direct_effects_covered" in kinds(covered, "notes")
    net = verify_outcome(code_mode=[cm(confined_guarantees(network_isolated=False))], changeset={"verdict": "proved"})
    assert net["outcome"] == UNCERTAIN


def test_code_mode_failures_refusals_and_interruptions():
    assert verify_outcome(code_mode=[cm(confined_guarantees(), exit_code=1, error="Traceback")])["outcome"] == UNVERIFIED
    refused = verify_outcome(code_mode=[{"exit_code": 126, "refused": True, "error": "no container backend",
                                         "runtime_guarantees": {"mode": "not_executed"}}])
    assert refused["outcome"] == UNVERIFIED and kinds(refused) == ["code_mode_refused"]
    timed = verify_outcome(code_mode=[cm(confined_guarantees(), receipt={"terminated_by": "timeout"})])
    assert timed["outcome"] == UNCERTAIN and "code_mode_timeout" in kinds(timed)
    nested = verify_outcome(code_mode=[cm(confined_guarantees(), tool_outcomes={"calls": 2, "counts": {"outcome_unknown": 1}})])
    assert nested["outcome"] == UNCERTAIN and "code_mode_nested_unconfirmed" in kinds(nested)


def test_direct_effects_table():
    assert ov.code_mode_direct_effects(cm(HOST))["observed"] is False
    assert ov.code_mode_direct_effects(cm(confined_guarantees()))["observed"] is True
    assert ov.code_mode_direct_effects(None)["observed"] is False
    assert ov.code_mode_direct_effects({"refused": True})["scope"] == "none"


@pytest.mark.asyncio
async def test_run_code_mode_attaches_the_classification_on_the_host_runtime(code_mode_host_runtime, tmp_path):
    from src.code_mode.runner import run_code_mode
    result = await run_code_mode("print('hi')", workspace=str(tmp_path))
    assert result["exit_code"] == 0, result
    ver = result["outcome_verification"]
    assert ver["outcome"] == "uncertain" and "code_mode_direct_effects_unobserved" in kinds(ver)


@pytest.mark.asyncio
async def test_run_code_mode_refusal_carries_unverified(monkeypatch, tmp_path):
    from src.code_mode import confined, runner
    monkeypatch.setattr(confined, "probe", lambda image=None, docker="docker": {
        "ok": False, "reason": "backend_unavailable", "detail": "the docker daemon did not answer"})
    result = await runner.run_code_mode("print('x')", workspace=str(tmp_path), runtime="confined")
    assert result.get("refused") is True
    assert result["outcome_verification"]["outcome"] == "unverified"
    assert kinds(result["outcome_verification"]) == ["code_mode_refused"]


@pytest.mark.asyncio
async def test_run_code_mode_confined_read_only_is_verified_in_a_real_container(container_test_image, tmp_path):
    from src.code_mode.runner import run_code_mode
    result = await run_code_mode("print('ok')", workspace=str(tmp_path), runtime="confined",
                                 image=container_test_image)
    assert result["exit_code"] == 0, result
    assert result["outcome_verification"]["outcome"] == "verified", result["outcome_verification"]
    wrote = await run_code_mode("print('ok')", workspace=str(tmp_path), runtime="confined",
                                image=container_test_image, workspace_access="read_write")
    assert wrote["outcome_verification"]["outcome"] == "uncertain"


@pytest.mark.asyncio
async def test_the_setting_off_leaves_the_result_as_it_was(code_mode_host_runtime, tmp_path, monkeypatch):
    from src.code_mode.runner import run_code_mode
    monkeypatch.setattr(ov, "enabled", lambda: False)
    result = await run_code_mode("print('hi')", workspace=str(tmp_path))
    assert "outcome_verification" not in result


# ── workers ────────────────────────────────────────────────────────────────

def test_worker_done_with_observed_changes_and_passing_verification_is_verified():
    v = verify_outcome(workers=[{
        "runner": "job", "status": "done", "exit_code": 0, "claimed_only": [],
        "changes": {"modified": ["a.py"]}, "verification": {"ran": True, "ok": True, "summary": "3 passed"},
        "proof": {"verdict": "proved", "uncertainty": []}}])
    assert v["outcome"] == VERIFIED, v
    assert {"worker_finished", "worker_changes_observed", "tests_passed", "changeset_proved"} <= set(kinds(v, "positive"))


def test_worker_claims_that_are_not_on_disk_are_unverified():
    v = verify_outcome(workers=[{"runner": "w", "status": "done", "claimed_only": ["ghost.py"],
                                 "changes": {"modified": ["a.py"]}}])
    assert v["outcome"] == UNVERIFIED and "worker_claims_not_on_disk" in kinds(v)
    assert "ghost.py" in v["reasons"][0]["detail"]


@pytest.mark.parametrize("row,outcome,kind", [
    ({"runner": "w", "status": "error", "error": "exited with code 2"}, UNVERIFIED, "worker_failed"),
    ({"runner": "w", "status": "done", "exit_code": 1}, UNVERIFIED, "worker_failed"),
    ({"runner": "w", "status": "timeout"}, UNCERTAIN, "worker_timed_out"),
    ({"runner": "w", "status": "cancelled"}, UNCERTAIN, "worker_cancelled"),
    ({"runner": "w", "cancelled": True, "status": "done"}, UNCERTAIN, "worker_cancelled"),
    ({"runner": "w", "status": "done", "unguarded": True, "exit_code": 0}, UNCERTAIN, "worker_unguarded"),
    ({"runner": "w", "status": "done", "exit_code": 0, "unguarded": False, "gate": {"gated": True}}, VERIFIED, "worker_finished"),
    ({"runner": "w", "status": "weird"}, UNCERTAIN, "worker_state_unknown"),
])
def test_worker_results(row, outcome, kind):
    v = verify_outcome(workers=[row])
    assert v["outcome"] == outcome, v
    assert kind in kinds(v) + kinds(v, "positive")


def test_an_unguarded_proof_uncertainty_makes_a_worker_uncertain():
    v = verify_outcome(workers=[{"runner": "job", "status": "done", "changes": {"modified": ["a"]},
                                 "proof": {"verdict": "proved",
                                           "uncertainty": [{"kind": "external_agent_unguarded", "detail": "x"}]}}])
    assert v["outcome"] == UNCERTAIN and "worker_unguarded" in kinds(v)


def test_dispatch_compact_attaches_the_classification(monkeypatch):
    from src import dispatch
    job = dispatch.DispatchJob("luis", {"tasks": []}, "/ws", "", "m", None, "t")
    job.status = "done"
    job.result = {"subagents": [{"name": "w1", "status": "done", "mutations": ["a.py"]}], "exit_code": 0}
    job.changes = {"modified": ["a.py"], "added": [], "deleted": [], "checkpoint": "abc"}
    job.verification = {"ran": True, "ok": True, "summary": "ok"}
    ver = dispatch.compact(job)["result"]["outcome_verification"]
    assert ver["outcome"] == "verified", ver

    job.verification = {"ran": True, "ok": False, "summary": "1 failed"}
    assert dispatch.compact(job)["result"]["outcome_verification"]["outcome"] == "unverified"

    job.verification = None
    job.result = {"subagents": [{"name": "w1", "status": "done", "mutations": ["ghost.py"]}], "exit_code": 0}
    ver = dispatch.compact(job)["result"]["outcome_verification"]
    assert ver["outcome"] == "unverified" and "worker_claims_not_on_disk" in kinds(ver)

    job.status = "running"
    assert "outcome_verification" not in dispatch.compact(job)["result"], "a running job has no outcome yet"

    job.status = "done"
    monkeypatch.setattr(ov, "enabled", lambda: False)
    assert "outcome_verification" not in dispatch.compact(job)["result"]


def test_external_worker_result_carries_its_own_classification(monkeypatch, tmp_path):
    from src import external_worker
    out = {"status": "done", "exit_code": 0, "ok": True, "unguarded": True, "runner": "x"}
    assert ov.verify_worker(out)["outcome"] == "uncertain"
    failed = ov.verify_worker({**out, "status": "error", "ok": False, "exit_code": 2, "error": "exited with code 2"})
    assert failed["outcome"] == "unverified"
    refusal = external_worker._fail("nope", "no such runner")
    assert ov.verify_worker(refusal)["outcome"] == "unverified"


# ── the harness ledger and the completion gate ─────────────────────────────

def ledger():
    from src.agent_harness import TurnLedger
    return TurnLedger(workspace=None, user_text="do the thing")


def test_ledger_events_keep_the_evidence_the_verifier_reads():
    led = ledger()
    led.record("run_code", "{}", {"exit_code": 0, "runtime_guarantees": HOST, "calls_made": 0})
    led.record("process_read", "{}", {"handle": "p1", "state": "lost", "exit_code": None,
                                      "termination_reason": "server restarted"})
    assert led.events[0]["code_mode"]["runtime_guarantees"]["mode"] == "host_process"
    assert led.events[1]["process"]["state"] == "lost"
    v = led.verify_outcome()
    assert v["outcome"] == "uncertain"
    assert {"code_mode_direct_effects_unobserved", "process_lost"} <= set(kinds(v))


def test_ledger_turn_with_a_mutation_and_passing_tests_is_verified():
    led = ledger()
    led.record("edit_file", json.dumps({"path": "a.py", "old_string": "a", "new_string": "b"}), {"ok": True})
    led.tests = {"ran": True, "ok": True, "summary": "5 passed"}
    assert led.verify_outcome()["outcome"] == "verified"
    led.tests = {"ran": True, "ok": False, "summary": "1 failed"}
    assert led.verify_outcome()["outcome"] == "unverified"


def test_check_completion_carries_the_classification_beside_its_own_verdict():
    led = ledger()
    led.record("read_file", json.dumps({"path": "a.py"}), {"ok": True, "content": "x"})
    check = led.check_completion("I have updated a.py and fixed the bug.")
    assert check["ok"] is False and "claims_without_mutation" in check["reasons"]
    assert check["verification"]["outcome"] == "unverified"
    assert "claim_unsupported" in kinds(check["verification"])
    led2 = ledger()
    led2.record("edit_file", json.dumps({"path": "a.py", "old_string": "a", "new_string": "b"}), {"ok": True})
    ok = led2.check_completion("I have updated a.py.")
    assert ok["ok"] is True and ok["verification"]["outcome"] == "verified"


def test_completion_gate_is_backed_by_the_same_function():
    from src.agent_harness import completion_gate
    assert completion_gate({"stop_reason": "complete", "tests": {"ran": True, "ok": False}}) == "tests_failed"
    assert completion_gate({"stop_reason": "complete", "ui_smoke": {"ran": True, "ok": False}}) == "ui_smoke_failed"
    assert completion_gate({"stop_reason": "complete"}, {"verdict": "contradicted"}) == "changeset_contradicted"
    # doubts do not gate; only findings of failure do
    assert completion_gate({"stop_reason": "complete", "tests": {"ran": True, "ok": False, "inconclusive": True}}) is None
    assert completion_gate({"stop_reason": "complete"}, {"verdict": "partial"}) is None
    assert completion_gate({"stop_reason": "complete_unverified", "tests": {"ran": True, "ok": False}}) is None


def test_verify_turn_adapter_and_malformed_evidence_never_raise():
    v = ov.verify_turn({"tests": {"ran": True, "ok": True}}, tool_results=[{"tool": "bash", "ok": True}])
    assert v["outcome"] == VERIFIED
    junk = verify_outcome(tool_results="not a list", tests=7, processes=[None, 3], code_mode=["x"],
                          workers=[[]], changeset="nope", completion=12)
    assert junk["outcome"] in (UNVERIFIED, UNCERTAIN)
    assert ov.verify_code_mode(None)["outcome"] == UNVERIFIED and ov.verify_worker(3)["outcome"] == UNVERIFIED


def test_the_setting_exists_and_defaults_on():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["agent_outcome_verification"] is True
    assert ov.enabled() is True
