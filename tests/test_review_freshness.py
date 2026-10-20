"""Execute the real review closure with real TurnLedger records, no model I/O."""
import ast
import asyncio
import json
import logging
from pathlib import Path

import pytest

from src import auto_review
from src.agent_harness import TurnLedger
from src.extra_round_policy import ExtraRounds


def _write(ledger, workspace, value, *, effect=False, ok=True):
    if ok:
        (workspace / "fixture.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
    ledger.record("mcp__own__update_item" if effect else "edit_file",
                  json.dumps({"path": "fixture.py", "new_string": str(value)}),
                  {"exit_code": 0 if ok else 1})


def _review_result(error=False):
    return {"model": "own-reviewer", "verdict": "defects" if error else "ok",
            "findings": [{"severity": "error", "file": "fixture.py", "line": 1,
                          "issue": "Own fixture mapping is wrong", "grounded": True}] if error else []}


def _closure(ledger, workspace, monkeypatch, *, errors=(), limit=3, reviewer="own-reviewer", action=None):
    source = Path(__file__).resolve().parents[1] / "src" / "agent_loop.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    branch = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                  and "_reviewer_model" in ast.unparse(node.test)
                  and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                          and call.func.attr == "review_turn" for call in ast.walk(node)))
    calls = []

    async def review(**kwargs):
        calls.append({"source": (workspace / "fixture.py").read_text(encoding="utf-8"),
                      "epoch": ledger.mutation_epoch, "kwargs": kwargs})
        if action:
            result = action(len(calls))
            if isinstance(result, Exception):
                raise result
            if result is None:
                return None
        return _review_result(errors[min(len(calls) - 1, len(errors) - 1)] if errors else False)

    monkeypatch.setattr(auto_review, "review_turn", review)
    function = ast.AsyncFunctionDef(name="close", args=ast.arguments(
        posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]), decorator_list=[],
        body=[ast.Global(names=["full_response"]), ast.For(target=ast.Name(id="round_num", ctx=ast.Store()),
                      iter=ast.List(elts=[ast.Constant(1)], ctx=ast.Load()), body=[branch], orelse=[])])
    policy = ExtraRounds("enforce")
    namespace = dict(_ledger=ledger, workspace=str(workspace), _reviewer_model=reviewer,
                     _auto_review=auto_review, _last_user="Correct fixture mapping", endpoint_url="http://cpu.invalid/v1",
                     model="own-writer", headers={}, workload="background", _review_max_fix=limit,
                     _xr=policy, round_response="", messages=[], full_response="", _lang_note=lambda text: text,
                     logger=logging.getLogger(__name__), json=json)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(source), "exec"), namespace)

    async def finish():
        return [json.loads(event.removeprefix("data: ")) async for event in namespace["close"]()]

    return calls, lambda: asyncio.run(finish()), policy


def test_real_ledger_counts_repeated_path_edits_as_distinct_events(tmp_path):
    ledger = TurnLedger(str(tmp_path))
    _write(ledger, tmp_path, 1)
    _write(ledger, tmp_path, 2)
    assert ledger.mutated_paths() == ["fixture.py"]
    assert len(ledger.mutations) == 2 and ledger.mutation_epoch == 2


def test_new_source_after_ok_is_reviewed_and_old_ok_is_not_current(tmp_path, monkeypatch):
    ledger = TurnLedger(str(tmp_path))
    calls, close, _ = _closure(ledger, tmp_path, monkeypatch)
    _write(ledger, tmp_path, 1)
    close()
    _write(ledger, tmp_path, 2)
    assert ledger.summary()["review"] is None
    close()
    assert [call["epoch"] for call in calls] == [1, 2]
    assert ledger.review["source_epoch"] == ledger.mutation_epoch


def test_unchanged_ok_and_reads_do_not_repeat_review(tmp_path, monkeypatch):
    ledger = TurnLedger(str(tmp_path))
    calls, close, _ = _closure(ledger, tmp_path, monkeypatch)
    _write(ledger, tmp_path, 1)
    close()
    ledger.record("read_file", '{"path":"fixture.py"}', {"exit_code": 0})
    close()
    assert len(calls) == 1


def test_same_path_real_edits_can_use_three_repairs_and_four_reviews(tmp_path, monkeypatch):
    ledger = TurnLedger(str(tmp_path))
    calls, close, policy = _closure(ledger, tmp_path, monkeypatch, errors=[True, True, True, False])
    for value in range(1, 5):
        _write(ledger, tmp_path, value)
        close()
    assert len(calls) == 4 and ledger.review_runs == 4
    assert ledger.review_fix_rounds == 3
    assert ledger.review["verdict"] == "ok" and not ledger.review.get("disputed")
    assert [row["budget_used"] for row in policy.records] == [1, 2, 3]


def test_same_path_edit_after_feedback_is_not_disputed(tmp_path, monkeypatch):
    ledger = TurnLedger(str(tmp_path))
    calls, close, _ = _closure(ledger, tmp_path, monkeypatch, errors=[True])
    _write(ledger, tmp_path, 1)
    close()
    _write(ledger, tmp_path, 2)
    close()
    assert len(calls) == 2 and ledger.review_fix_rounds == 2
    assert not ledger.review.get("disputed")


def test_known_effect_with_real_new_source_is_not_disputed(tmp_path, monkeypatch):
    ledger = TurnLedger(str(tmp_path))
    calls, close, _ = _closure(ledger, tmp_path, monkeypatch, errors=[True])
    _write(ledger, tmp_path, 1)
    close()
    _write(ledger, tmp_path, 2, effect=True)
    assert len(ledger.mutations) == 1 and ledger.mutation_epoch == 2
    close()
    assert len(calls) == 2 and calls[0]["source"] != calls[1]["source"]
    assert not ledger.review.get("disputed")
    assert ledger.review_fix_rounds == 2


def test_no_mutation_after_feedback_is_disputed_without_second_call(tmp_path, monkeypatch):
    ledger = TurnLedger(str(tmp_path))
    calls, close, _ = _closure(ledger, tmp_path, monkeypatch, errors=[True])
    _write(ledger, tmp_path, 1)
    close()
    close()
    close()
    assert len(calls) == 1 and ledger.review_fix_rounds == 1
    assert ledger.review["disputed"] is True and ledger.review["findings"]


@pytest.mark.parametrize("limit", [0, 1, 3])
def test_explicit_repair_limit_is_preserved_but_current_source_still_reviewed(tmp_path, monkeypatch, limit):
    ledger = TurnLedger(str(tmp_path))
    calls, close, policy = _closure(ledger, tmp_path, monkeypatch, errors=[True], limit=limit)
    for value in range(limit + 1):
        _write(ledger, tmp_path, value)
        close()
    assert len(calls) == limit + 1
    assert ledger.review_fix_rounds == limit and len(policy.records) == limit
    assert ledger.review["source_epoch"] == ledger.mutation_epoch


@pytest.mark.parametrize("failure", [OSError("own startup failure"), TimeoutError("own timeout"), None])
def test_failed_attempt_does_not_loop_on_same_source_or_revive_previous_ok(tmp_path, monkeypatch, failure):
    ledger = TurnLedger(str(tmp_path))
    def action(index):
        return failure if index == 2 else True
    calls, close, _ = _closure(ledger, tmp_path, monkeypatch, action=action)
    _write(ledger, tmp_path, 1)
    close()
    _write(ledger, tmp_path, 2)
    close()
    close()
    assert len(calls) == 2 and ledger.review is None
    _write(ledger, tmp_path, 3)
    close()
    assert len(calls) == 3 and ledger.review["source_epoch"] == 3


def test_mutation_during_await_cannot_stamp_old_review_as_current(tmp_path, monkeypatch):
    ledger = TurnLedger(str(tmp_path))
    def action(index):
        if index == 1:
            _write(ledger, tmp_path, 2)
        return True
    calls, close, policy = _closure(ledger, tmp_path, monkeypatch, errors=[True, False], action=action)
    _write(ledger, tmp_path, 1)
    close()
    assert ledger.review is None and ledger.review_fix_rounds == 0 and not policy.records
    close()
    assert [call["epoch"] for call in calls] == [1, 2]
    assert ledger.review["verdict"] == "ok" and ledger.review["source_epoch"] == 2


def test_failed_write_does_not_invalidate_current_review(tmp_path, monkeypatch):
    ledger = TurnLedger(str(tmp_path))
    calls, close, _ = _closure(ledger, tmp_path, monkeypatch)
    _write(ledger, tmp_path, 1)
    close()
    before = dict(ledger.review)
    _write(ledger, tmp_path, 2, ok=False)
    close()
    assert len(calls) == 1 and ledger.review == before


@pytest.mark.parametrize("reviewer,receipt", [(None, None), (None, {"verdict": "skipped", "summary": "existing decision"})])
def test_disabled_or_skipped_review_configuration_is_unchanged(tmp_path, monkeypatch, reviewer, receipt):
    ledger = TurnLedger(str(tmp_path))
    ledger.review = receipt
    calls, close, _ = _closure(ledger, tmp_path, monkeypatch, reviewer=reviewer)
    _write(ledger, tmp_path, 1)
    close()
    assert not calls and ledger.review == receipt


def test_legacy_approval_snapshot_restores_effects_not_a_fresh_review(tmp_path, monkeypatch):
    original = TurnLedger(str(tmp_path))
    _write(original, tmp_path, 1)
    ledger = TurnLedger(str(tmp_path))
    ledger.restore_approval_evidence(original.approval_evidence())
    calls, close, _ = _closure(ledger, tmp_path, monkeypatch)
    close()
    assert len(calls) == 1 and ledger.review["source_epoch"] == ledger.mutation_epoch


def _stream_review(monkeypatch, tmp_path, *, mode="enforce", limit=3, unattended=False, stop=False):
    from src import agent_loop as loop, model_context
    from tests.test_agent_harness_functional import _patch_common, _real_edit, _edit_call, _events
    (tmp_path / "fixture.py").write_text("VALUE = 0\n", encoding="utf-8")
    options = {"agent_project_tests": False, "agent_auto_review": "own-reviewer",
               "agent_auto_review_fix_rounds": limit, "agent_extra_round_policy": mode,
               "agent_checkpoints": False, "agent_static_analysis": "off", "agent_ui_smoke": False,
               "agent_completion_engine": False, "agent_completion_engine_shadow": False}
    _patch_common(monkeypatch, settings=options, tool_exec=_real_edit(str(tmp_path)))
    monkeypatch.setattr(model_context, "_query_context_length", lambda *a, **k: (32768, True))
    reviews = []

    async def review(**kwargs):
        value = int((tmp_path / "fixture.py").read_text(encoding="utf-8").split("=")[1])
        reviews.append(value)
        return _review_result(value < 4)

    monkeypatch.setattr(auto_review, "review_turn", review)
    state = {"value": 0, "feedback": 0}

    async def scripted(_candidates, messages, **kwargs):
        feedback = sum(bool(message.get("_harness_note")) and "independent review" in str(message.get("content"))
                       for message in messages)
        if state["value"] == 0 or feedback > state["feedback"]:
            old = state["value"]
            state["value"] += 1
            text, finish = _edit_call("fixture.py", f"VALUE = {old}", f"VALUE = {state['value']}"), "tool_calls"
        else:
            text, finish = "He actualizado fixture.py.", "stop"
        state["feedback"] = feedback
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": finish})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(loop, "stream_llm_with_fallback", scripted)

    async def run():
        return [event async for event in loop.stream_agent_loop(
            "http://cpu-fixture.invalid/v1", "own-writer",
            [{"role": "user", "content": "Actualiza fixture.py y comprueba el resultado."}],
            workspace=str(tmp_path), owner="own1010", session_id="own1010", max_rounds=30,
            unattended=unattended, relevant_tools={"edit_file"},
            pending_cancel=lambda: "own Stop" if stop and len(reviews) >= 2 else None,
            harness_options={"trusted_workspace": str(tmp_path), "review_model": "own-reviewer",
                             "run_tests": False, "checkpoint": False, "no_memory": True,
                             "no_skills": True, "repo_map": False})]

    events = _events(asyncio.run(run()))
    summary = next(event["data"] for event in reversed(events) if event.get("type") == "harness_summary")
    issues = [event for event in events if event.get("type") == "harness_check" and event.get("status") == "review_issues"]
    return reviews, summary, issues, events


@pytest.mark.parametrize("mode", ["legacy", "shadow", "enforce"])
@pytest.mark.parametrize("unattended", [False, True])
def test_actual_stream_four_current_reviews_obey_explicit_three_repairs(monkeypatch, tmp_path, mode, unattended):
    reviews, summary, issues, events = _stream_review(monkeypatch, tmp_path, mode=mode, unattended=unattended)
    assert reviews == [1, 2, 3, 4]
    assert summary["review_fix_rounds"] == 3 and summary["review"]["verdict"] == "ok"
    assert summary["review"]["source_epoch"] == 4 and not summary["review"].get("disputed")
    assert [(event["attempt"], event["max_attempts"]) for event in issues] == [(1, 3), (2, 3), (3, 3)]
    metrics = next(event["data"]["harness"] for event in events if event.get("type") == "metrics")
    assert metrics["review"] == summary["review"] and summary["stop_reason"] == "complete"


@pytest.mark.parametrize("limit", [0, 1])
def test_actual_stream_zero_and_one_repair_limit(monkeypatch, tmp_path, limit):
    reviews, summary, issues, _ = _stream_review(monkeypatch, tmp_path, limit=limit)
    assert reviews == list(range(1, limit + 2))
    assert summary["review_fix_rounds"] == limit and len(issues) == limit
    assert summary["review"]["verdict"] == "defects" and not summary["review"].get("disputed")


@pytest.mark.parametrize("unattended", [False, True])
def test_actual_stream_stop_still_cancels_repairs(monkeypatch, tmp_path, unattended):
    reviews, summary, issues, _ = _stream_review(monkeypatch, tmp_path, unattended=unattended, stop=True)
    assert reviews == [1, 2] and summary["stop_reason"] == "cancelled"
