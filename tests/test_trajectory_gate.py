"""Tests for src/trajectory_gate.py — declarative, CI-style assertions over
a recorded agent run.

Split into: (1) each check pass+fail on synthetic `Trajectory` objects built
by hand (no disk, no agent_runs involved — `evaluate()` is pure), (2) the
`require_observation_before` ordering check's two rule shapes, (3)
`evaluate_recent`'s aggregation over several synthetic runs, (4) CLI exit
codes, and (5) the route's owner scoping.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src import trajectory_gate as tg
from src.trajectory_gate import Step, Trajectory, evaluate


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _tool_call(tool, command="", ts=None):
    return Step(kind="tool_call", tool=tool, detail=command, ts=ts, signature=f"{tool}::{command}")


def _tool_result(tool, ok=True, blocked=False, ts=None):
    return Step(kind="tool_result", tool=tool, ok=ok, blocked=blocked, ts=ts)


def _model_call(tokens=100, duration_ms=500.0, ok=True, ts=None):
    return Step(kind="model_call", tool="qwen", ok=ok, tokens=tokens, duration_ms=duration_ms, ts=ts)


def _traj(steps, *, status="finished", final_text="done", start_ts=0.0, finish_ts=10.0):
    return Trajectory(
        run_id="run1", session_id="sess1", status=status,
        start_ts=start_ts, finish_ts=finish_ts, final_text=final_text, steps=steps,
    )


# ---------------------------------------------------------------------------
# individual checks — pass + fail
# ---------------------------------------------------------------------------

def _check(report, cid):
    for c in report["checks"]:
        if c["id"] == cid:
            return c
    raise AssertionError(f"check {cid} not in report")


def test_max_error_rate_pass_and_fail():
    ok_traj = _traj([_tool_call("bash"), _tool_result("bash", ok=True)])
    bad_traj = _traj([_tool_call("bash"), _tool_result("bash", ok=False), Step(kind="error")])

    passing = evaluate(ok_traj, {"max_error_rate": 0.5})
    failing = evaluate(bad_traj, {"max_error_rate": 0.1})

    assert _check(passing, "max_error_rate")["ok"] is True
    assert _check(failing, "max_error_rate")["ok"] is False


def test_max_steps_pass_and_fail():
    traj = _traj([_tool_call("a"), _tool_call("b"), _tool_call("c")])
    assert _check(evaluate(traj, {"max_steps": 5}), "max_steps")["ok"] is True
    assert _check(evaluate(traj, {"max_steps": 2}), "max_steps")["ok"] is False


def test_max_tool_calls_pass_and_fail():
    traj = _traj([_tool_call("a"), _tool_call("b"), _model_call()])
    r_pass = _check(evaluate(traj, {"max_tool_calls": 2}), "max_tool_calls")
    r_fail = _check(evaluate(traj, {"max_tool_calls": 1}), "max_tool_calls")
    assert r_pass["ok"] is True and r_pass["actual"] == 2
    assert r_fail["ok"] is False


def test_max_duration_s_uses_wall_clock_window_when_available():
    traj = _traj([_tool_call("a")], start_ts=100.0, finish_ts=130.0)
    r_pass = _check(evaluate(traj, {"max_duration_s": 60}), "max_duration_s")
    r_fail = _check(evaluate(traj, {"max_duration_s": 10}), "max_duration_s")
    assert r_pass["ok"] is True and r_pass["actual"] == 30.0
    assert r_fail["ok"] is False


def test_max_duration_s_falls_back_to_summed_model_call_durations():
    traj = _traj([_model_call(duration_ms=2000.0), _model_call(duration_ms=3000.0)],
                 start_ts=None, finish_ts=None)
    r = _check(evaluate(traj, {"max_duration_s": 4}), "max_duration_s")
    assert r["actual"] == 5.0
    assert r["ok"] is False


def test_max_tokens_pass_and_fail():
    traj = _traj([_model_call(tokens=1000), _model_call(tokens=500)])
    r_pass = _check(evaluate(traj, {"max_tokens": 2000}), "max_tokens")
    r_fail = _check(evaluate(traj, {"max_tokens": 1000}), "max_tokens")
    assert r_pass["ok"] is True and r_pass["actual"] == 1500
    assert r_fail["ok"] is False


def test_forbid_tools_pass_and_fail():
    traj = _traj([_tool_call("bash"), _tool_call("read_file")])
    r_pass = _check(evaluate(traj, {"forbid_tools": ["rm_*"]}), "forbid_tools")
    r_fail = _check(evaluate(traj, {"forbid_tools": ["bash"]}), "forbid_tools")
    assert r_pass["ok"] is True and r_pass["actual"] == []
    assert r_fail["ok"] is False and r_fail["actual"] == ["bash"]


def test_require_tools_pass_and_fail():
    traj = _traj([_tool_call("bash"), _tool_call("read_file")])
    r_pass = _check(evaluate(traj, {"require_tools": ["bash"]}), "require_tools")
    r_fail = _check(evaluate(traj, {"require_tools": ["run_tests"]}), "require_tools")
    assert r_pass["ok"] is True
    assert r_fail["ok"] is False and r_fail["actual"] == ["run_tests"]


def test_no_repeated_identical_calls_pass_and_fail():
    traj = _traj([_tool_call("bash", "ls"), _tool_call("bash", "ls"), _tool_call("bash", "ls")])
    r_pass = _check(evaluate(traj, {"no_repeated_identical_calls": 5}), "no_repeated_identical_calls")
    r_fail = _check(evaluate(traj, {"no_repeated_identical_calls": 2}), "no_repeated_identical_calls")
    assert r_pass["ok"] is True
    assert r_fail["ok"] is False and r_fail["actual"] == 3


def test_final_answer_required_pass_and_fail():
    with_final = _traj([Step(kind="final")], final_text="")
    without_final = _traj([_tool_call("bash")], final_text="")
    assert _check(evaluate(with_final, {"final_answer_required": True}), "final_answer_required")["ok"] is True
    assert _check(evaluate(without_final, {"final_answer_required": True}), "final_answer_required")["ok"] is False


def test_final_answer_required_accepts_accumulated_text_without_explicit_final_step():
    traj = _traj([_tool_call("bash")], final_text="here is the answer")
    assert _check(evaluate(traj, {"final_answer_required": True}), "final_answer_required")["ok"] is True


def test_max_blocked_calls_pass_and_fail():
    traj = _traj([_tool_result("rm", blocked=True), _tool_result("bash", blocked=False)])
    r_pass = _check(evaluate(traj, {"max_blocked_calls": 2}), "max_blocked_calls")
    r_fail = _check(evaluate(traj, {"max_blocked_calls": 0}), "max_blocked_calls")
    assert r_pass["ok"] is True and r_pass["actual"] == 1
    assert r_fail["ok"] is False


# ---------------------------------------------------------------------------
# require_observation_before — both rule shapes, pass + fail + ordering
# ---------------------------------------------------------------------------

def test_require_before_rule_passes_when_read_precedes_write():
    traj = _traj([_tool_call("read_file"), _tool_call("write_file")])
    spec = {"require_observation_before": [{"tool": "write_file", "requires_before": ["read_file"]}]}
    assert _check(evaluate(traj, spec), "require_observation_before")["ok"] is True


def test_require_before_rule_fails_when_write_has_no_prior_read():
    traj = _traj([_tool_call("write_file"), _tool_call("read_file")])
    spec = {"require_observation_before": [{"tool": "write_file", "requires_before": ["read_file"]}]}
    r = _check(evaluate(traj, spec), "require_observation_before")
    assert r["ok"] is False
    assert "write_file" in r["actual"][0]


def test_require_after_last_of_rule_passes_when_tests_run_after_last_edit():
    traj = _traj([_tool_call("edit_file"), _tool_call("edit_file"), _tool_call("run_tests")])
    spec = {"require_observation_before": [{"after_last_of": "edit_file", "requires_after": ["run_tests"]}]}
    assert _check(evaluate(traj, spec), "require_observation_before")["ok"] is True


def test_require_after_last_of_rule_fails_when_nothing_runs_after_last_edit():
    traj = _traj([_tool_call("run_tests"), _tool_call("edit_file")])
    spec = {"require_observation_before": [{"after_last_of": "edit_file", "requires_after": ["run_tests"]}]}
    r = _check(evaluate(traj, spec), "require_observation_before")
    assert r["ok"] is False


def test_require_after_last_of_rule_is_a_noop_when_the_pattern_never_occurred():
    traj = _traj([_tool_call("bash")])
    spec = {"require_observation_before": [{"after_last_of": "edit_file", "requires_after": ["run_tests"]}]}
    assert _check(evaluate(traj, spec), "require_observation_before")["ok"] is True


# ---------------------------------------------------------------------------
# aggregation — evaluate_recent
# ---------------------------------------------------------------------------

def test_evaluate_recent_aggregates_pass_rate_per_check(monkeypatch):
    passing = _traj([_tool_call("bash"), Step(kind="final")])
    failing = _traj([_tool_call("bash")] * 5, final_text="")

    def _fake_recent_ids(limit, since):
        return ["run-a", "run-b", "run-c"]

    def _fake_evaluate_run(run_id, spec):
        traj = passing if run_id != "run-c" else failing
        return evaluate(traj, spec)

    monkeypatch.setattr(tg, "_recent_run_ids", _fake_recent_ids)
    monkeypatch.setattr(tg, "evaluate_run", _fake_evaluate_run)

    agg = tg.evaluate_recent({"max_tool_calls": 2, "final_answer_required": True}, limit=3)

    assert agg["runs_evaluated"] == 3
    assert agg["runs_passed"] == 2
    assert agg["runs_failed"] == 1
    assert agg["per_check_pass_rate"]["max_tool_calls"] == pytest.approx(2 / 3, abs=1e-4)
    assert agg["per_check_pass_rate"]["final_answer_required"] == pytest.approx(2 / 3, abs=1e-4)


def test_evaluate_recent_counts_unreadable_runs_without_dropping_silently(monkeypatch):
    monkeypatch.setattr(tg, "_recent_run_ids", lambda limit, since: ["run-a", "run-bad"])

    def _fake_evaluate_run(run_id, spec):
        if run_id == "run-bad":
            raise LookupError("boom")
        return evaluate(_traj([Step(kind="final")]), spec)

    monkeypatch.setattr(tg, "evaluate_run", _fake_evaluate_run)
    agg = tg.evaluate_recent({"final_answer_required": True}, limit=2)
    assert agg["runs_evaluated"] == 1
    assert agg["runs_unreadable"] == 1


# ---------------------------------------------------------------------------
# spec parsing
# ---------------------------------------------------------------------------

def test_parse_spec_json():
    spec = tg.parse_spec(json.dumps({"max_steps": 10}))
    assert spec == {"max_steps": 10}


def test_parse_spec_yaml():
    spec = tg.parse_spec("max_steps: 10\nforbid_tools:\n  - rm_*\n", filename="spec.yaml")
    assert spec == {"max_steps": 10, "forbid_tools": ["rm_*"]}


# ---------------------------------------------------------------------------
# CLI exit codes
# ---------------------------------------------------------------------------

def test_cli_run_exits_zero_on_pass(tmp_path, monkeypatch):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"max_steps": 10}))
    monkeypatch.setattr(tg, "evaluate_run", lambda run_id, spec: {"ok": True, "run_id": run_id, "session_id": "s", "status": "finished", "step_count": 1, "checks": []})

    code = tg.main(["--run", "sess1", "--spec", str(spec_path)])
    assert code == 0


def test_cli_run_exits_one_on_fail(tmp_path, monkeypatch):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"max_steps": 1}))
    monkeypatch.setattr(tg, "evaluate_run", lambda run_id, spec: {"ok": False, "run_id": run_id, "session_id": "s", "status": "finished", "step_count": 5, "checks": [{"id": "max_steps", "ok": False, "expected": 1, "actual": 5, "detail": ""}]})

    code = tg.main(["--run", "sess1", "--spec", str(spec_path)])
    assert code == 1


def test_cli_run_exits_two_when_run_not_found(tmp_path, monkeypatch, capsys):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"max_steps": 1}))

    def _boom(run_id, spec):
        raise LookupError(f"no run found for {run_id!r}")

    monkeypatch.setattr(tg, "evaluate_run", _boom)
    code = tg.main(["--run", "nope", "--spec", str(spec_path)])
    assert code == 2
    assert "no run found" in capsys.readouterr().err


def test_cli_recent_exits_one_when_any_run_fails(tmp_path, monkeypatch):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"max_steps": 1}))
    monkeypatch.setattr(tg, "evaluate_recent", lambda spec, limit, since: {
        "runs_evaluated": 2, "runs_unreadable": 0, "runs_passed": 1, "runs_failed": 1,
        "pass_rate": 0.5, "per_check_pass_rate": {}, "reports": [],
    })
    code = tg.main(["--recent", "20", "--spec", str(spec_path)])
    assert code == 1


def test_cli_recent_exits_zero_when_all_pass(tmp_path, monkeypatch):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"max_steps": 1}))
    monkeypatch.setattr(tg, "evaluate_recent", lambda spec, limit, since: {
        "runs_evaluated": 2, "runs_unreadable": 0, "runs_passed": 2, "runs_failed": 0,
        "pass_rate": 1.0, "per_check_pass_rate": {}, "reports": [],
    })
    code = tg.main(["--recent", "20", "--spec", str(spec_path)])
    assert code == 0


def test_cli_requires_run_or_recent(tmp_path):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({}))
    with pytest.raises(SystemExit) as excinfo:
        tg.main(["--spec", str(spec_path)])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# _model_call_steps: exact run_id attribution over the clock-window fallback
# ---------------------------------------------------------------------------

def test_model_call_steps_prefers_exact_run_id_over_the_clock_window(monkeypatch):
    """The regression this closes: two overlapping runs of the same session
    (two tabs, an immediate regeneration) used to both fall inside the same
    clock window and get merged. A record carrying `run_id` is now matched
    exactly -- and excluded if it names a DIFFERENT run, even though its
    timestamp sits inside this run's window."""
    from src import llm_trace

    records = [
        {"ts": 100.0, "run_id": "run-a", "model": "qwen-a", "duration_ms": 500},
        # Same window as run-a, but a different run's own record -- must be
        # excluded now that exact attribution is possible.
        {"ts": 100.4, "run_id": "run-b", "model": "qwen-b", "duration_ms": 500},
        # No run_id at all (older trace, or a call made outside any
        # stream_agent_loop context) -- falls back to the clock window.
        {"ts": 100.6, "run_id": None, "model": "qwen-legacy-in-window", "duration_ms": 500},
        {"ts": 500.0, "run_id": None, "model": "qwen-legacy-out-of-window", "duration_ms": 500},
    ]
    monkeypatch.setattr(llm_trace, "_iter_records", lambda session_id: iter(records))

    steps = tg._model_call_steps("sess1", "run-a", 99.0, 102.0)
    tools = sorted(s.tool for s in steps)
    assert tools == ["qwen-a", "qwen-legacy-in-window"]


def test_model_call_steps_falls_back_to_the_window_when_run_id_is_unknown(monkeypatch):
    """`load_trajectory` always resolves SOME run_id today, but as a
    defense-in-depth the window fallback still works when the caller has
    none -- every record with no `run_id` of its own is judged purely by
    the clock window, same as before this lote."""
    from src import llm_trace

    records = [
        {"ts": 100.0, "run_id": None, "model": "qwen-in-window", "duration_ms": 500},
        {"ts": 500.0, "run_id": None, "model": "qwen-out-of-window", "duration_ms": 500},
    ]
    monkeypatch.setattr(llm_trace, "_iter_records", lambda session_id: iter(records))

    steps = tg._model_call_steps("sess1", None, 99.0, 102.0)
    assert [s.tool for s in steps] == ["qwen-in-window"]


# ---------------------------------------------------------------------------
# route owner scoping
# ---------------------------------------------------------------------------

def _build_app(monkeypatch, *, resolved_session="sess1", verify_raises=None, traj=None, load_raises=None):
    import routes.trajectory_gate_routes as gate_routes

    def _resolve(run_id):
        return resolved_session

    def _load(run_id):
        if load_raises is not None:
            raise load_raises
        return traj or _traj([Step(kind="final")])

    def _verify(request, session_id):
        if verify_raises is not None:
            raise verify_raises

    monkeypatch.setattr(tg, "_resolve_session_id", _resolve)
    monkeypatch.setattr(tg, "load_trajectory", _load)
    monkeypatch.setattr(gate_routes, "_verify_session_owner", _verify)

    app = FastAPI()
    app.include_router(gate_routes.setup_trajectory_gate_routes())
    return app


def test_get_gate_for_a_session_the_caller_does_not_own_is_rejected(monkeypatch):
    app = _build_app(monkeypatch, verify_raises=HTTPException(404, "Session not found"))
    response = TestClient(app).get("/api/agent-runs/run1/gate")
    assert response.status_code == 404


def test_get_gate_unknown_run_is_404_not_500(monkeypatch):
    app = _build_app(monkeypatch, resolved_session=None)
    response = TestClient(app).get("/api/agent-runs/run1/gate")
    assert response.status_code == 404


def test_get_gate_uses_default_spec_for_an_owned_run(monkeypatch):
    traj = _traj([_tool_call("a") for _ in range(1)] + [Step(kind="final")])
    app = _build_app(monkeypatch, traj=traj)
    response = TestClient(app).get("/api/agent-runs/run1/gate")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert any(c["id"] == "final_answer_required" for c in body["checks"])


def test_post_gate_uses_caller_supplied_spec(monkeypatch):
    traj = _traj([_tool_call("a"), _tool_call("b"), _tool_call("c")], final_text="")
    app = _build_app(monkeypatch, traj=traj)
    response = TestClient(app).post("/api/agent-runs/run1/gate", json={"max_tool_calls": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    c = [x for x in body["checks"] if x["id"] == "max_tool_calls"][0]
    assert c["actual"] == 3 and c["ok"] is False
