"""OBJ-47: `plan_done` runs the typed goals of a plan task before sealing it.

Fakes everywhere that matters: the harness test runner and the HTTP probe are
replaced at their seams (`plan_goals._run_spec`, `plan_goals._http_probe`), the
tracker persists to a pytest tmp dir, and nothing touches a model or the
network. Two tests at the end run a REAL `pytest` subprocess in a tmp workspace
through `project_tests.run_tests` (no shell, no network) to prove the wiring
end to end.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from src import plan_goals as pg
from src import plan_tracker as pt
from src.tool_policy import ToolPolicy


PLAN = """
## WP01 - Notes service

Build the service.

Acceptance:
- the tests pass

test_passes: pytest tests/test_notes.py -q
http_ok: https://example.com/health 200

## WP02 - Docs

Write the README.

- **file_exists**: README.md

## WP03 - Plain task

Nothing typed here.

Acceptance:
- it works
"""


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "DATA_DIR", str(tmp_path / "data"), raising=False)
    monkeypatch.setattr(pt, "PLAN_TRACKER_DIR", str(tmp_path / "data" / "plan_tracker"))
    # Goals on, default timeout, unless a test says otherwise.
    monkeypatch.setattr(pg, "_setting", lambda key, default: default)
    yield


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_notes.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr("src.tool_execution.get_active_workspace", lambda: str(ws))
    # A fake interpreter / runner: resolution must not depend on this machine.
    from src import project_tests
    monkeypatch.setattr(project_tests, "_python_for", lambda w: None)
    monkeypatch.setattr(project_tests, "_fallback_python", lambda w="": "/fake/python")
    monkeypatch.setattr(pg, "_which", lambda name: "/fake/" + name)
    return str(ws)


def _run(coro):
    return asyncio.run(coro)


def _passing(**extra):
    res = {"ran": True, "ok": True, "exit_code": 0, "timed_out": False, "duration_s": 0.4,
           "summary": "3 passed", "output_tail": "3 passed in 0.2s", "inconclusive": False}
    res.update(extra)
    return res


def _failing(**extra):
    res = {"ran": True, "ok": False, "exit_code": 1, "timed_out": False, "duration_s": 0.9,
           "summary": "1 failed", "inconclusive": False,
           "output_tail": "FAILED tests/test_notes.py::test_ok - assert 1 == 2\n1 failed in 0.1s"}
    res.update(extra)
    return res


def _forbidden_runner(*_a, **_k):
    raise AssertionError("the runner must not be called")


def _goal(kind, **spec):
    return {"id": "g1", "kind": kind, "spec": spec, "raw": kind}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_extract_goals_reads_typed_lines_only():
    text = (
        "Intro prose that mentions test_passes in passing.\n"
        "- test_passes: `pytest tests/a.py -q`\n"
        "http_ok: <https://example.com/ok> expect 204\n"
        "* [ ] **http_ok**: https://example.com/other\n"
        "test_passes: pytest tests/a.py -q\n"            # duplicate of the first
        "custom_check: some_tool\n"
        "not_a_kind: whatever\n"
    )
    goals = pg.extract_goals(text)
    assert [g["kind"] for g in goals] == ["test_passes", "http_ok", "http_ok", "custom_check"]
    assert goals[0]["spec"] == {"cmd": "pytest tests/a.py -q"}
    assert goals[1]["spec"] == {"url": "https://example.com/ok", "expect": 204}
    assert goals[2]["spec"] == {"url": "https://example.com/other"}
    assert [g["id"] for g in goals] == ["g1", "g2", "g3", "g4"]


def test_parse_plan_attaches_goals_to_their_task_and_bumps_the_parser():
    spec = pt.parse_plan("plan.md", PLAN)
    wp1, wp2, wp3 = spec.tasks
    assert [g["kind"] for g in wp1.goals] == ["test_passes", "http_ok"]
    assert [g["kind"] for g in wp2.goals] == ["file_exists"]
    assert wp3.goals == []
    assert pt.PARSER_VERSION >= 4
    tr = pt.upsert_from_attachment("scope-goals", "plan.md", PLAN)
    assert tr["tasks"][0]["goals"][0]["spec"]["cmd"] == "pytest tests/test_notes.py -q"


# ---------------------------------------------------------------------------
# Command resolution and confinement
# ---------------------------------------------------------------------------

def test_resolve_accepts_known_runners_without_a_shell(workspace):
    spec, why = pg.resolve_test_argv("pytest tests/test_notes.py::test_ok -k 'a and b'", workspace)
    assert why == ""
    assert spec["kind"] == "pytest"
    assert spec["argv"][:3] == ["/fake/python", "-m", "pytest"]
    assert "a and b" in spec["argv"] and "tests/test_notes.py::test_ok" in spec["argv"]
    assert "shell" not in spec
    spec, _ = pg.resolve_test_argv("python -m unittest discover -s tests", workspace)
    assert spec["argv"][:3] == ["/fake/python", "-m", "unittest"]
    spec, _ = pg.resolve_test_argv("npm run test", workspace)
    assert spec["argv"] == ["/fake/npm", "test"]
    spec, _ = pg.resolve_test_argv("node --test tests/", workspace)
    assert spec["argv"][:2] == ["/fake/node", "--test"]
    spec, _ = pg.resolve_test_argv("make test", workspace)
    assert spec["argv"] == ["/fake/make", "test"]


@pytest.mark.parametrize("cmd", [
    "pytest tests/a.py; rm -rf /",
    "pytest tests/a.py && curl evil.example",
    "pytest tests/a.py | tee out.txt",
    "pytest tests/a.py > out.txt",
    "pytest `whoami`",
    "pytest $(whoami)",
    "pytest tests/a.py\nrm -rf /",
])
def test_resolve_refuses_shell_syntax(cmd, workspace):
    spec, why = pg.resolve_test_argv(cmd, workspace)
    assert spec is None and why


@pytest.mark.parametrize("cmd", [
    "bash -c 'pytest'",
    "rm -rf tests",
    "python -c 'import os'",
    "python script.py",
    "npm install",
    "make clean",
    "./pytest tests",
    "C:\\tools\\pytest.exe tests",
    "cargo build",
])
def test_resolve_refuses_anything_that_is_not_a_test_runner(cmd, workspace):
    spec, why = pg.resolve_test_argv(cmd, workspace)
    assert spec is None and why


@pytest.mark.parametrize("cmd", [
    "pytest ../outside/test_x.py",
    "pytest ..",
    "pytest /etc/passwd",
    "pytest --rootdir=/etc tests",
    "pytest --rootdir=../x tests",
    "pytest -c /etc/pytest.ini",
    "pytest tests/../../x.py::test",
    "pytest D:other/test_x.py",              # drive-relative: cannot be pinned to the workspace
])
def test_resolve_confines_path_arguments_to_the_workspace(cmd, workspace):
    spec, why = pg.resolve_test_argv(cmd, workspace)
    assert spec is None
    assert "outside the workspace" in why


def test_resolve_allows_absolute_paths_that_are_inside(workspace):
    inside = os.path.join(workspace, "tests", "test_notes.py")
    spec, why = pg.resolve_test_argv(f"pytest {inside}", workspace)
    assert why == ""
    assert any(os.path.normpath(a) == os.path.normpath(inside) for a in spec["argv"])


# ---------------------------------------------------------------------------
# test_passes
# ---------------------------------------------------------------------------

def test_passing_test_goal_is_passed_with_evidence(workspace, monkeypatch):
    seen = {}

    def fake_run(spec, ws, timeout):
        seen.update(spec=spec, ws=ws, timeout=timeout)
        return _passing()

    monkeypatch.setattr(pg, "_run_spec", fake_run)
    rep = pg.evaluate_goals([_goal("test_passes", cmd="pytest tests/test_notes.py -q")], workspace=workspace)
    assert rep["ok"] is True and rep["passed"] == 1 and rep["failed"] == 0
    r = rep["results"][0]
    assert r["status"] == "passed" and "exit 0" in r["evidence"] and "3 passed" in r["evidence"]
    assert r["exit_code"] == 0
    assert seen["ws"] == workspace and seen["spec"]["argv"][2] == "pytest"


def test_failing_test_goal_carries_exit_code_and_last_lines(workspace, monkeypatch):
    monkeypatch.setattr(pg, "_run_spec", lambda s, w, t: _failing())
    rep = pg.evaluate_goals([_goal("test_passes", cmd="pytest tests/test_notes.py")], workspace=workspace)
    assert rep["ok"] is False and rep["failed"] == 1
    r = rep["results"][0]
    assert r["status"] == "failed" and r["exit_code"] == 1
    assert "exit 1" in r["evidence"] and "assert 1 == 2" in r["evidence"]
    assert "[failed] test_passes" in pg.format_failures(rep)


def test_pytest_collecting_nothing_is_not_a_pass(workspace, monkeypatch):
    monkeypatch.setattr(pg, "_run_spec", lambda s, w, t: _passing(exit_code=5, ok=True, inconclusive=True,
                                                                  summary="no tests collected"))
    rep = pg.evaluate_goals([_goal("test_passes", cmd="pytest tests/none")], workspace=workspace)
    assert rep["results"][0]["status"] == "failed"


def test_timeout_is_a_failure_and_the_bound_is_clamped(workspace, monkeypatch):
    got = []

    def fake_run(spec, ws, timeout):
        got.append(timeout)
        return {"ran": True, "ok": False, "exit_code": None, "timed_out": True, "inconclusive": True,
                "duration_s": timeout, "summary": "timed out", "output_tail": ""}

    monkeypatch.setattr(pg, "_run_spec", fake_run)
    monkeypatch.setattr(pg, "_setting", lambda key, default: 99999 if key.endswith("timeout_seconds") else default)
    rep = pg.evaluate_goals([_goal("test_passes", cmd="pytest tests/test_notes.py")], workspace=workspace)
    assert got == [pg.MAX_GOAL_TIMEOUT_S]
    r = rep["results"][0]
    assert r["status"] == "failed" and r.get("timed_out") is True and "timed out" in r["evidence"]
    monkeypatch.setattr(pg, "_setting", lambda key, default: 1 if key.endswith("timeout_seconds") else default)
    got.clear()
    pg.evaluate_goals([_goal("test_passes", cmd="pytest tests/test_notes.py")], workspace=workspace)
    assert got == [pg.MIN_GOAL_TIMEOUT_S]


def test_total_budget_bounds_the_whole_call(workspace, monkeypatch):
    got = []
    clock = [1000.0]
    monkeypatch.setattr(pg.time, "monotonic", lambda: clock[0])

    def fake_run(spec, ws, timeout):
        got.append(timeout)
        clock[0] += 20.0                       # each run "takes" 20 s
        return _passing()

    monkeypatch.setattr(pg, "_run_spec", fake_run)
    goals = [dict(_goal("test_passes", cmd="pytest tests/test_notes.py"), id=f"g{i}") for i in range(3)]
    rep = pg.evaluate_goals(goals, workspace=workspace, timeout_s=120, total_budget_s=25)
    assert rep["passed"] == 1 and rep["skipped"] == 2
    assert got and got[0] <= 25
    assert "budget" in rep["results"][1]["evidence"]


def test_runner_that_could_not_start_fails_the_goal(workspace, monkeypatch):
    monkeypatch.setattr(pg, "_run_spec", lambda s, w, t: {
        "ran": False, "inconclusive": True, "summary": "could not run: [Errno 2] no such file"})
    rep = pg.evaluate_goals([_goal("test_passes", cmd="pytest tests")], workspace=workspace)
    assert rep["results"][0]["status"] == "failed"
    assert "could not run" in rep["results"][0]["evidence"]
    monkeypatch.setattr(pg, "_run_spec", lambda s, w, t: (_ for _ in ()).throw(RuntimeError("boom")))
    rep = pg.evaluate_goals([_goal("test_passes", cmd="pytest tests")], workspace=workspace)
    assert rep["results"][0]["status"] == "failed" and "boom" in rep["results"][0]["evidence"]


def test_no_workspace_means_skipped_not_run(monkeypatch):
    monkeypatch.setattr(pg, "_run_spec", _forbidden_runner)
    rep = pg.evaluate_goals([_goal("test_passes", cmd="pytest tests")], workspace=None)
    r = rep["results"][0]
    assert r["status"] == "skipped" and "no workspace" in r["reason"]
    assert rep["ok"] is True and rep["unverified"] == 1


def test_path_outside_workspace_is_refused_and_never_run(workspace, monkeypatch):
    monkeypatch.setattr(pg, "_run_spec", _forbidden_runner)
    rep = pg.evaluate_goals([_goal("test_passes", cmd="pytest ../other/test_x.py")], workspace=workspace)
    r = rep["results"][0]
    assert r["status"] == "skipped" and "outside the workspace" in r["reason"]


def test_read_only_policy_blocks_the_test_goal(workspace, monkeypatch):
    monkeypatch.setattr(pg, "_run_spec", _forbidden_runner)
    rep = pg.evaluate_goals([_goal("test_passes", cmd="pytest tests/test_notes.py")],
                            workspace=workspace, policy=ToolPolicy(read_only=True))
    r = rep["results"][0]
    assert r["status"] == "skipped" and "policy" in r["reason"]


def test_disabled_bash_or_guide_only_policy_blocks_the_test_goal(workspace, monkeypatch):
    monkeypatch.setattr(pg, "_run_spec", _forbidden_runner)
    for policy in (ToolPolicy(disabled_tools=frozenset({"bash"})), ToolPolicy(block_all_tool_calls=True)):
        rep = pg.evaluate_goals([_goal("test_passes", cmd="pytest tests/test_notes.py")],
                                workspace=workspace, policy=policy)
        assert rep["results"][0]["status"] == "skipped"


def test_a_broken_policy_object_fails_closed(workspace, monkeypatch):
    class Broken:
        def blocks_action(self, *a, **k):
            raise RuntimeError("nope")

    monkeypatch.setattr(pg, "_run_spec", _forbidden_runner)
    rep = pg.evaluate_goals([_goal("test_passes", cmd="pytest tests/test_notes.py")],
                            workspace=workspace, policy=Broken())
    assert rep["results"][0]["status"] == "skipped"


def test_required_sandbox_confinement_blocks_host_execution(workspace, monkeypatch):
    from src import sandbox_exec
    monkeypatch.setattr(sandbox_exec, "confinement_required", lambda: True)
    monkeypatch.setattr(pg, "_run_spec", _forbidden_runner)
    rep = pg.evaluate_goals([_goal("test_passes", cmd="pytest tests/test_notes.py")], workspace=workspace)
    r = rep["results"][0]
    assert r["status"] == "skipped" and "confinement" in r["reason"]


# ---------------------------------------------------------------------------
# http_ok
# ---------------------------------------------------------------------------

def test_http_ok_passes_on_expected_status(monkeypatch):
    calls = []
    monkeypatch.setattr(pg, "_http_probe", lambda url, timeout: calls.append((url, timeout)) or {"status": 200})
    rep = pg.evaluate_goals([_goal("http_ok", url="https://example.com/health", expect=200)], workspace=None)
    r = rep["results"][0]
    assert r["status"] == "passed" and r["status_code"] == 200 and "200" in r["evidence"]
    assert calls[0][1] <= pg.DEFAULT_HTTP_TIMEOUT_S


def test_http_ok_default_accepts_2xx_3xx_and_rejects_others(monkeypatch):
    for status, expected in ((204, "passed"), (302, "passed"), (404, "failed"), (500, "failed")):
        monkeypatch.setattr(pg, "_http_probe", lambda url, timeout, s=status: {"status": s})
        rep = pg.evaluate_goals([_goal("http_ok", url="https://example.com/x")], workspace=None)
        assert rep["results"][0]["status"] == expected, status


def test_http_ok_wrong_status_and_transport_error_fail(monkeypatch):
    monkeypatch.setattr(pg, "_http_probe", lambda url, timeout: {"status": 200})
    rep = pg.evaluate_goals([_goal("http_ok", url="https://example.com/x", expect=201)], workspace=None)
    assert rep["results"][0]["status"] == "failed" and "expected 201" in rep["results"][0]["evidence"]
    monkeypatch.setattr(pg, "_http_probe", lambda url, timeout: {"error": "ConnectError: refused"})
    rep = pg.evaluate_goals([_goal("http_ok", url="https://example.com/x")], workspace=None)
    assert rep["results"][0]["status"] == "failed" and "ConnectError" in rep["results"][0]["evidence"]


def test_http_ok_non_public_destinations_are_refused_by_the_real_broker():
    # No probe fake: the real SSRF preflight must refuse these without a request.
    for url in ("http://127.0.0.1:8000/health", "http://localhost/health", "http://192.168.1.10/x",
                "http://169.254.169.254/latest/meta-data"):
        rep = pg.evaluate_goals([_goal("http_ok", url=url)], workspace=None)
        r = rep["results"][0]
        assert r["status"] == "skipped", url
        assert "outbound HTTP policy" in r["reason"]
        assert rep["ok"] is True


def test_http_ok_scheme_and_policy(monkeypatch):
    monkeypatch.setattr(pg, "_http_probe", lambda url, timeout: pytest.fail("must not fetch"))
    rep = pg.evaluate_goals([_goal("http_ok", url="file:///etc/passwd")], workspace=None)
    assert rep["results"][0]["status"] == "skipped"
    rep = pg.evaluate_goals([_goal("http_ok", url="https://example.com/x")], workspace=None,
                            policy=ToolPolicy(disabled_tools=frozenset({"web_fetch"})))
    assert rep["results"][0]["status"] == "skipped"


def test_http_probe_maps_the_broker_result(monkeypatch):
    from src import outbound_fetch as ofx

    class Got:
        status_code = 503

    seen = {}
    monkeypatch.setattr(ofx, "classify_destination", lambda url, **k: ("public", []))
    monkeypatch.setattr(ofx, "fetch", lambda url, **k: seen.update(k) or Got())
    assert pg._http_probe("https://example.com/x", 7.0) == {"status": 503}
    assert seen["profile"] == ofx.PUBLIC_UNTRUSTED and seen["timeout"] == 7.0 and seen["max_redirects"] == 3
    assert "allow_local" not in seen
    monkeypatch.setattr(ofx, "fetch", lambda url, **k: (_ for _ in ()).throw(ofx.BodyTooLargeError(url, 9, 1)))
    assert "error" in pg._http_probe("https://example.com/x", 7.0)


# ---------------------------------------------------------------------------
# Other kinds
# ---------------------------------------------------------------------------

def test_unknown_or_unrunnable_kinds_are_skipped_with_a_reason(workspace, monkeypatch):
    monkeypatch.setattr(pg, "_run_spec", _forbidden_runner)
    goals = [_goal("custom_check", tool="x"), _goal("file_exists", path="README.md"),
             {"id": "g9", "kind": "telepathy", "spec": {}}, {"id": "g10", "spec": {}}]
    rep = pg.evaluate_goals(goals, workspace=workspace)
    assert [r["status"] for r in rep["results"]] == ["skipped"] * 4
    assert all("not checkable automatically" in r["reason"] for r in rep["results"])
    assert rep["ok"] is True and rep["passed"] == 0 and rep["unverified"] == 4


# ---------------------------------------------------------------------------
# plan_done
# ---------------------------------------------------------------------------

def _ctx(project_id):
    return {"project_id": project_id, "turn": 1}


def _setup(scope):
    tr = pt.upsert_from_attachment(scope, "plan.md", PLAN)
    return tr, tr["tasks"][0]


def _done(task_id, ctx):
    from src.agent_tools.plan_tools import PlanDoneTool
    return _run(PlanDoneTool().execute(
        json.dumps({"id": task_id, "evidence": "ran the suite and probed the health URL"}), ctx))


def _state(scope, tr, tid):
    return pt.load(scope, tr["hash"])["state"][tid]


def test_plan_done_seals_when_every_goal_passes(workspace, monkeypatch):
    monkeypatch.setattr(pg, "_run_spec", lambda s, w, t: _passing())
    monkeypatch.setattr(pg, "_http_probe", lambda url, timeout: {"status": 200})
    tr, wp1 = _setup("scope-pass")
    res = _done(wp1["id"], _ctx("scope-pass"))
    assert res["exit_code"] == 0 and res["sealed"] is True
    assert res["goals"]["passed"] == 2 and res["goals"]["failed"] == 0
    assert "2 typed goal(s) passed" in res["output"]
    st = _state("scope-pass", tr, wp1["id"])
    assert st["status"] == "done" and st["goals"]["ok"] is True and st["goals"]["passed"] == 2
    assert len(st["goals"]["results"]) == 2


def test_plan_done_does_not_seal_while_a_goal_fails_and_seals_after_the_fix(workspace, monkeypatch):
    runs = {"n": 0}

    def fake_run(spec, ws, timeout):
        runs["n"] += 1
        return _failing() if runs["n"] == 1 else _passing()

    monkeypatch.setattr(pg, "_run_spec", fake_run)
    monkeypatch.setattr(pg, "_http_probe", lambda url, timeout: {"status": 200})
    tr, wp1 = _setup("scope-fail")
    res = _done(wp1["id"], _ctx("scope-fail"))
    assert res["exit_code"] == 1 and res["sealed"] is False
    assert "NOT sealed" in res["error"] and "assert 1 == 2" in res["error"] and "exit 1" in res["error"]
    assert res["goals"]["failed"] == 1 and res["goals"]["passed"] == 1
    assert res["progress"]["done"] == 0
    st = _state("scope-fail", tr, wp1["id"])
    assert st["status"] == "pending"                       # still open
    assert st["goals"]["ok"] is False and st["goals"]["failed"] == 1
    # The model fixes the code and calls plan_done again: goals run again.
    res2 = _done(wp1["id"], _ctx("scope-fail"))
    assert res2["exit_code"] == 0 and res2["sealed"] is True and runs["n"] == 2
    assert _state("scope-fail", tr, wp1["id"])["status"] == "done"


def test_plan_done_reports_skipped_goals_as_not_verified(workspace, monkeypatch):
    monkeypatch.setattr(pg, "_run_spec", lambda s, w, t: _passing())
    tr, wp1 = _setup("scope-skip")
    # The URL is a public name but the real preflight needs DNS: force a refusal-free skip
    # through the policy instead, so no resolver runs.
    ctx = dict(_ctx("scope-skip"), tool_policy=ToolPolicy(disabled_tools=frozenset({"web_fetch"})))
    res = _done(wp1["id"], ctx)
    assert res["exit_code"] == 0 and res["sealed"] is True
    assert res["goals"]["passed"] == 1 and res["goals"]["skipped"] == 1
    assert "NOT verified" in res["output"] and "http_ok" in res["output"]


def test_plan_done_with_only_unrunnable_goals_seals_but_says_so(workspace, monkeypatch):
    monkeypatch.setattr(pg, "_run_spec", _forbidden_runner)
    tr, _ = _setup("scope-fileexists")
    wp2 = tr["tasks"][1]
    res = _done(wp2["id"], _ctx("scope-fileexists"))
    assert res["exit_code"] == 0 and res["sealed"] is True
    assert res["goals"]["skipped"] == 1 and "not checkable automatically" in res["output"]


def test_plan_done_without_goals_is_unchanged(workspace, monkeypatch):
    monkeypatch.setattr(pg, "_run_spec", _forbidden_runner)
    tr, _ = _setup("scope-plain")
    wp3 = tr["tasks"][2]
    res = _done(wp3["id"], _ctx("scope-plain"))
    assert res["exit_code"] == 0 and "goals" not in res and res["sealed"] is True


def test_plan_done_ignores_goals_when_the_setting_is_off(workspace, monkeypatch):
    monkeypatch.setattr(pg, "_run_spec", _forbidden_runner)
    monkeypatch.setattr(pg, "_setting", lambda key, default: False if key == "agent_plan_goals" else default)
    tr, wp1 = _setup("scope-off")
    res = _done(wp1["id"], _ctx("scope-off"))
    assert res["exit_code"] == 0 and "goals" not in res


def test_plan_done_runs_goals_under_the_turns_read_only_policy(workspace, monkeypatch):
    monkeypatch.setattr(pg, "_run_spec", _forbidden_runner)
    monkeypatch.setattr(pg, "_http_probe", lambda url, timeout: {"status": 200})
    tr, wp1 = _setup("scope-ro")
    ctx = dict(_ctx("scope-ro"), tool_policy=ToolPolicy(read_only=True))
    res = _done(wp1["id"], ctx)
    assert res["exit_code"] == 0
    kinds = {r["kind"]: r["status"] for r in res["goals"]["results"]}
    # The policy that forbids the agent's own bash / web_fetch forbids the goals too.
    assert kinds == {"test_passes": "skipped", "http_ok": "skipped"}
    assert res["goals"]["passed"] == 0 and "NOT verified" in res["output"]


def test_plan_task_lists_the_typed_goals(workspace):
    from src.agent_tools.plan_tools import PlanTaskTool
    tr, wp1 = _setup("scope-task")
    out = _run(PlanTaskTool().execute(json.dumps({"id": wp1["id"]}), _ctx("scope-task")))
    assert "Typed goals" in out["output"] and "pytest tests/test_notes.py -q" in out["output"]


# ---------------------------------------------------------------------------
# reconcile must not bypass the goals
# ---------------------------------------------------------------------------

def test_reconcile_does_not_close_a_task_whose_goals_have_not_passed(workspace, monkeypatch):
    tr, wp1 = _setup("scope-rec")
    todos = [{"content": "WP01 Notes service", "status": "completed"},
             {"content": "WP03 Plain task", "status": "completed"}]
    marked = pt.reconcile("scope-rec", tr, todos=todos, mutated_paths=["x.py"], workspace=workspace, turn=1)
    assert marked == [tr["tasks"][2]["id"]]                # the goal-less task only
    # A failed plan_done run keeps it held; a passing one is what seals it.
    monkeypatch.setattr(pg, "_run_spec", lambda s, w, t: _failing())
    monkeypatch.setattr(pg, "_http_probe", lambda url, timeout: {"status": 200})
    assert _done(wp1["id"], _ctx("scope-rec"))["sealed"] is False
    tr2 = pt.load("scope-rec", tr["hash"])
    assert pt.reconcile("scope-rec", tr2, todos=todos, mutated_paths=["x.py"], workspace=workspace, turn=1) == []


# ---------------------------------------------------------------------------
# Real subprocess, real runner (no shell, no network, no model)
# ---------------------------------------------------------------------------

@pytest.fixture
def real_runner(monkeypatch):
    from src import project_tests
    monkeypatch.setattr(project_tests, "_python_for", lambda w: None)
    monkeypatch.setattr(project_tests, "_fallback_python", lambda w="": sys.executable)


def test_real_pytest_goal_passes_and_fails_through_the_harness_runner(tmp_path, real_runner):
    ws = tmp_path / "real_ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_good.py").write_text("def test_good():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    (ws / "tests" / "test_bad.py").write_text("def test_bad():\n    assert 1 + 1 == 3\n", encoding="utf-8")
    rep = pg.evaluate_goals(
        [dict(_goal("test_passes", cmd="pytest tests/test_good.py"), id="g1"),
         dict(_goal("test_passes", cmd="pytest tests/test_bad.py"), id="g2")],
        workspace=str(ws))
    good, bad = rep["results"]
    assert good["status"] == "passed", good
    assert bad["status"] == "failed" and bad["exit_code"] == 1 and "assert" in bad["evidence"], bad
    assert rep["ok"] is False


def test_real_runner_never_interprets_shell_syntax(tmp_path, real_runner):
    ws = tmp_path / "real_ws2"
    (ws / "tests").mkdir(parents=True)
    marker = ws / "pwned.txt"
    rep = pg.evaluate_goals(
        [_goal("test_passes", cmd=f"pytest tests/x.py; python -c \"open(r'{marker}','w').write('x')\"")],
        workspace=str(ws))
    assert rep["results"][0]["status"] == "skipped"
    assert not marker.exists()
