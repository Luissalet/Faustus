"""Execute the loop's actual project-test branch without importing the server."""
import ast
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent_harness import TurnLedger


def _project_test_branch(ledger, workspace, monkeypatch):
    from src import project_tests
    source = Path(__file__).resolve().parents[1] / "src" / "agent_loop.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    branch = next(n for n in ast.walk(tree) if isinstance(n, ast.If)
                  and "_tests_on" in ast.unparse(n.test) and "_ledger" in ast.unparse(n.test))
    calls = []

    def run(*args, **kwargs):
        current = (workspace / "fixture.txt").read_text(encoding="utf-8")
        calls.append(current)
        return {"ran": True, "ok": current != "broken", "summary": current}

    monkeypatch.setattr(project_tests, "detect_test_command", lambda *a: {"label": "own CPU check"})
    monkeypatch.setattr(project_tests, "run_for_turn", run)
    monkeypatch.setattr(project_tests, "compact", lambda value: value)
    fn = ast.AsyncFunctionDef(name="exercise", args=ast.arguments(
        posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]), decorator_list=[],
        body=[ast.For(target=ast.Name(id="round_num", ctx=ast.Store()),
                      iter=ast.List(elts=[ast.Constant(1)], ctx=ast.Load()), body=[branch], orelse=[])])
    namespace = dict(_ledger=ledger, workspace=str(workspace), _tests_on=True, _hopts={},
                     get_setting=lambda name, default=None: default, _last_user="", _context_turn_id="own",
                     _xr=SimpleNamespace(permits=lambda *a: False), _tests_max_fix=1,
                     logger=logging.getLogger(__name__), json=json, asyncio=asyncio)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), str(source), "exec"), namespace)

    async def finish():
        return [event async for event in namespace["exercise"]()]

    return calls, lambda: asyncio.run(finish())


def _write(ledger, workspace, value):
    (workspace / "fixture.txt").write_text(value, encoding="utf-8")
    ledger.record("write_file", json.dumps({"path": "fixture.txt", "content": value}), {"exit_code": 0})


def test_new_same_path_mutation_requires_fresh_check_after_three_runs(tmp_path, monkeypatch):
    ledger = TurnLedger(str(tmp_path))
    calls, finish = _project_test_branch(ledger, tmp_path, monkeypatch)
    for value in ["v1", "v2", "v3", "broken"]:
        _write(ledger, tmp_path, value)
        finish()
    assert calls == ["v1", "v2", "v3", "broken"]
    assert ledger.tests_runs == 4
    assert ledger.tests["ok"] is False
    assert ledger.verify_outcome()["outcome"] == "unverified"
    assert len(ledger.tests_history) == 4


def test_unchanged_source_does_not_repeat_project_check(tmp_path, monkeypatch):
    ledger = TurnLedger(str(tmp_path))
    calls, finish = _project_test_branch(ledger, tmp_path, monkeypatch)
    _write(ledger, tmp_path, "ok")
    finish()
    finish()
    ledger.record("read_file", '{"path":"fixture.txt"}', {"exit_code": 0})
    finish()
    assert calls == ["ok"]
    assert ledger.tests_runs == 1


@pytest.mark.parametrize("tool,args,result", [
    ("write_file", '{"path":"fixture.txt","content":"new"}', {"exit_code": 0}),
    ("mcp__own__update_item", "{}", {"ok": True}),
    ("delegate_agents", "{}", {"subagents": [{"name": "own", "mutations": ["fixture.txt"]}]}),
])
def test_new_known_effect_cannot_reuse_old_pass(tool, args, result, tmp_path, monkeypatch):
    ledger = TurnLedger(str(tmp_path))
    calls, finish = _project_test_branch(ledger, tmp_path, monkeypatch)
    _write(ledger, tmp_path, "ok")
    finish()
    old = dict(ledger.tests)
    ledger.record(tool, args, result)
    assert ledger.tests is None
    assert ledger.summary()["tests"] is None
    assert ledger.tests_history[0] == old


def test_failed_write_does_not_invalidate_observed_source(tmp_path, monkeypatch):
    ledger = TurnLedger(str(tmp_path))
    calls, finish = _project_test_branch(ledger, tmp_path, monkeypatch)
    _write(ledger, tmp_path, "ok")
    finish()
    ledger.record("write_file", '{"path":"fixture.txt","content":"new"}', {"exit_code": 1})
    finish()
    assert calls == ["ok"]
    assert ledger.tests["ok"] is True


def test_failed_fresh_check_cannot_revive_previous_pass(tmp_path, monkeypatch):
    from src import project_tests
    ledger = TurnLedger(str(tmp_path))
    calls, finish = _project_test_branch(ledger, tmp_path, monkeypatch)
    _write(ledger, tmp_path, "ok")
    finish()
    _write(ledger, tmp_path, "broken")
    def unavailable(*args, **kwargs):
        raise OSError("own checker could not start")
    monkeypatch.setattr(project_tests, "run_for_turn", unavailable)
    finish()
    assert ledger.tests is None
    assert len(ledger.tests_history) == 1
    assert ledger.tests_history[0]["source_epoch"] < ledger.mutation_epoch


@pytest.mark.parametrize("first", ["exception", "not_run", "no_result"])
def test_unexecuted_check_retries_at_next_completion_without_new_mutation(tmp_path, monkeypatch, first):
    from src import project_tests
    ledger = TurnLedger(str(tmp_path))
    calls, finish = _project_test_branch(ledger, tmp_path, monkeypatch)
    _write(ledger, tmp_path, "old")
    finish()
    old_epoch = ledger.tests["source_epoch"]
    _write(ledger, tmp_path, "new")
    attempts = []

    def transient(*args, **kwargs):
        attempts.append(ledger.mutation_epoch)
        if len(attempts) == 1:
            if first == "exception":
                raise OSError("own transient checker startup failure")
            return {"ran": False, "ok": None, "inconclusive": True} if first == "not_run" else None
        return {"ran": True, "ok": True, "summary": "own current check passed"}

    monkeypatch.setattr(project_tests, "run_for_turn", transient)
    finish()
    assert not (ledger.tests and ledger.tests.get("ran") and ledger.tests.get("ok")), "old PASS must not revive"
    assert len(ledger.tests_history) == 1
    finish()
    assert len(attempts) == 2
    assert ledger.tests["ok"] is True and ledger.tests["source_epoch"] == ledger.mutation_epoch
    assert ledger.tests["source_epoch"] > old_epoch
    assert len(ledger.tests_history) == 2
    assert not ledger.tests_need_run()


def test_executed_failure_is_not_repeated_on_unchanged_source(tmp_path, monkeypatch):
    ledger = TurnLedger(str(tmp_path))
    calls, finish = _project_test_branch(ledger, tmp_path, monkeypatch)
    _write(ledger, tmp_path, "broken")
    finish()
    finish()
    assert calls == ["broken"]
    assert ledger.tests["ran"] is True and ledger.tests["ok"] is False
    assert ledger.tests_runs == 1 and len(ledger.tests_history) == 1


def test_check_covers_only_its_starting_epoch(tmp_path, monkeypatch):
    from src import project_tests
    ledger = TurnLedger(str(tmp_path))
    calls, finish = _project_test_branch(ledger, tmp_path, monkeypatch)
    _write(ledger, tmp_path, "ok")
    def changed_during_check(*args, **kwargs):
        ledger.record("write_file", '{"path":"fixture.txt","content":"new"}', {"exit_code": 0})
        return {"ran": True, "ok": True}
    monkeypatch.setattr(project_tests, "run_for_turn", changed_during_check)
    finish()
    assert ledger.tests is None
    assert ledger.tests_need_run()
    assert len(ledger.tests_history) == 1
    assert ledger.tests_history[0]["source_epoch"] < ledger.mutation_epoch


def test_completed_turn_keeps_check_history_in_existing_metrics(tmp_path, monkeypatch):
    from src import project_tests
    from tests.test_agent_harness_functional import (
        _patch_common, _scripted_stream, _run, _real_edit, _edit_call,
    )
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _patch_common(monkeypatch, settings={"agent_project_tests": True}, tool_exec=_real_edit(str(tmp_path)))
    monkeypatch.setattr(project_tests, "detect_test_command", lambda *a: {"label": "own check"})
    monkeypatch.setattr(project_tests, "run_for_turn", lambda *a, **k: {"ran": True, "ok": True})
    _scripted_stream(monkeypatch, [(_edit_call("a.py", "x = 1", "x = 2"), "tool_calls"),
                                  ("He cambiado a.py.", "stop")])
    events = _run(str(tmp_path), user="Cambia x a 2 en a.py")
    summary = next(e["data"] for e in events if e.get("type") == "harness_summary")
    metrics = next(e["data"]["harness"] for e in events if e.get("type") == "metrics")
    assert len(metrics["tests_history"]) == 1
    assert metrics["tests_history"] == summary["tests_history"]
    assert metrics["tests_history"][-1] == metrics["tests"]
    assert metrics["tests"]["source_epoch"] == 1
