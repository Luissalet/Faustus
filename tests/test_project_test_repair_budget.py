"""Repair configuration and actual loop closes with an own stdlib checker."""
import ast
import asyncio
import json
from pathlib import Path
import sys

import pytest

from src import agent_loop as al, agent_settings_schema as schema, project_tests, settings
from src.extra_round_policy import ExtraRounds
from tests.test_agent_harness_functional import _patch_common, _real_edit, _edit_call, _events

KEY = "agent_project_tests_fix_rounds"


def test_auto_default_and_schema_roundtrip_preserve_explicit_limits():
    assert settings.DEFAULT_SETTINGS[KEY] == -1
    assert KEY not in settings.DEFAULT_BUMPS
    field = schema.field_for(KEY)
    assert (field["min"], field["max"]) == (-1, 5)
    assert "-1" in field["help"] and "0" in field["help"]
    assert settings.get_setting(KEY) == -1
    for value in (-1, 0, 1, 3):
        typed = schema.coerce_setting_value(KEY, str(value))
        assert typed == value
        settings.update_settings({KEY: typed})
        assert settings.get_setting(KEY) == value
        assert json.loads(Path(settings.SETTINGS_FILE).read_text(encoding="utf-8"))[KEY] == value


@pytest.mark.parametrize("raw,expected", [(None, -1), ("", -1), ("bad", -1), ({}, -1),
                                          (float("inf"), -1), (-2, -1), ("-1", -1),
                                          (0, 0), (False, 0), (True, 1), (1, 1), (3, 3), ("3", 3)])
def test_parse_fallback_is_auto_and_explicit_integers_keep_their_meaning(raw, expected):
    tree = ast.parse(Path(al.__file__).read_text(encoding="utf-8"))
    block = next(node for node in ast.walk(tree) if isinstance(node, ast.Try)
                 and any(isinstance(child, ast.Assign) and any(isinstance(t, ast.Name)
                         and t.id == "_tests_max_fix" for t in child.targets) for child in node.body))
    namespace = {"get_setting": lambda *args: raw}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[block], type_ignores=[])), al.__file__, "exec"), namespace)
    assert namespace["_tests_max_fix"] == expected


def _exercise(monkeypatch, tmp_path, *, configured=None, unattended=False, mode="enforce",
              initial_budget=30, stop=False, no_edit=False, not_run=False):
    workspace = tmp_path / "own-project"
    workspace.mkdir()
    (workspace / "fixture.py").write_text("VALUE = 0\n", encoding="utf-8")
    (workspace / "checker.py").write_text(
        "import pathlib, sys\n"
        "value = int(pathlib.Path('fixture.py').read_text().split('=')[1])\n"
        "print('own value', value, 'expected 6')\n"
        "sys.exit(0 if value == 6 else 1)\n", encoding="utf-8")
    options = {"agent_project_tests": True, "agent_extra_round_policy": mode,
               "agent_static_analysis": "off", "agent_ui_smoke": False,
               "agent_checkpoints": False, "agent_auto_review": "off",
               "agent_auto_continue_cycles": 4, "agent_completion_engine": False,
               "agent_completion_engine_shadow": False}
    if configured is not None:
        options[KEY] = configured
    settings.update_settings(options)  # conftest supplies a private settings file
    _patch_common(monkeypatch, tool_exec=_real_edit(str(workspace)))
    monkeypatch.setattr(al, "get_setting", settings.get_setting)
    import src.model_context as context
    monkeypatch.setattr(context, "_query_context_length", lambda *a, **k: (32768, True))
    import httpx
    import requests
    import src.llm_core as llm
    outbound = []
    def no_http(*a, **k):
        outbound.append("HTTP/model attempt")
        raise AssertionError("CPU fixture must not call a live service")
    async def no_async(*a, **k):
        no_http(*a, **k)
    monkeypatch.setattr(httpx.Client, "send", no_http)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_async)
    monkeypatch.setattr(requests.Session, "request", no_http)
    monkeypatch.setattr(llm, "llm_call_async", no_async)
    provider = {"value": 0, "repairs_seen": 0}
    async def scripted(_candidates, messages, **kwargs):
        prompts = sum(m.get("_harness_note", False) and "tests FAILED" in str(m.get("content") or "") for m in messages)
        repair = prompts > provider["repairs_seen"]
        if provider["value"] == 0 or (repair and not no_edit):
            old = provider["value"]
            provider["value"] += 1
            text, finish = _edit_call("fixture.py", f"VALUE = {old}", f"VALUE = {provider['value']}"), "tool_calls"
        else:
            text, finish = "He actualizado fixture.py.", "stop"
        provider["repairs_seen"] = prompts
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": finish})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", scripted)
    checks, grants, recorded, limits_seen = [], [], [], []
    real_runner, real_permits, real_event = project_tests.run_for_turn, ExtraRounds.permits, ExtraRounds.event
    def runner(*args, **kwargs):
        result = {"ran": False, "ok": None, "inconclusive": True} if not_run else real_runner(*args, **kwargs)
        checks.append(result)
        return result
    def permits(policy, cause, used, limit):
        decision = real_permits(policy, cause, used, limit)
        if cause == "tests_fix":
            limits_seen.append(limit)
            if decision:
                grants.append((used + 1, limit))
        return decision
    def event(policy, cause, *, used, limit, **kwargs):
        if cause == "tests_fix":
            recorded.append((used, limit))
        return real_event(policy, cause, used=used, limit=limit, **kwargs)
    monkeypatch.setattr(project_tests, "run_for_turn", runner)
    monkeypatch.setattr(ExtraRounds, "permits", permits)
    monkeypatch.setattr(ExtraRounds, "event", event)
    async def run():
        return [chunk async for chunk in al.stream_agent_loop(
            "http://cpu-fixture.invalid/v1", "scripted-fixture",
            [{"role": "user", "content": "Actualiza fixture.py y comprueba el resultado."}],
            workspace=str(workspace), owner="own-repair991", session_id="own-repair991",
            max_rounds=initial_budget, unattended=unattended, relevant_tools={"edit_file"},
            pending_cancel=lambda: "own fixture Stop" if stop and len(checks) >= 2 else None,
            harness_options={"trusted_workspace": str(workspace), "run_tests": True,
                             "test_command": f'"{sys.executable}" -B checker.py', "checkpoint": False,
                             "no_memory": True, "no_skills": True, "review_model": "off", "repo_map": False})]
    events = _events(asyncio.run(run()))
    summary = next(e["data"] for e in reversed(events) if e.get("type") == "harness_summary")
    failures = [e for e in events if e.get("type") == "harness_check" and e.get("status") == "tests_failed"]
    assert not outbound
    assert grants == recorded == [(e["attempt"], e["max_attempts"]) for e in failures]
    if mode != "legacy":
        assert all(e["max_attempts"] == e["budget_limit"] for e in failures)
    assert all(limit >= 0 for limit in limits_seen), "never send raw AUTO to ExtraRounds"
    assert settings.get_setting(KEY) == (settings.DEFAULT_SETTINGS[KEY] if configured is None else configured)
    raw_settings = json.loads(Path(settings.SETTINGS_FILE).read_text(encoding="utf-8"))
    assert (KEY not in raw_settings) if configured is None else raw_settings[KEY] == configured
    return summary, checks, failures, events


@pytest.mark.parametrize("unattended", [False, True])
def test_actual_default_auto_reaches_pass_after_five_repairs(monkeypatch, tmp_path, unattended):
    summary, checks, failures, _ = _exercise(monkeypatch, tmp_path, unattended=unattended)
    assert len(checks) == 6 and summary["tests_fix_rounds"] == 5
    assert summary["tests"]["ok"] is True and summary["stop_reason"] == "complete"
    assert len(summary["tests_history"]) == 6
    assert all(e["max_attempts"] == 30 for e in failures)


@pytest.mark.parametrize("mode", ["enforce", "shadow", "legacy"])
def test_auto_tracks_actual_accumulated_budget_extensions(monkeypatch, tmp_path, mode):
    summary, checks, failures, events = _exercise(monkeypatch, tmp_path, configured=-1, mode=mode, initial_budget=4)
    assert len(checks) == 6 and summary["tests"]["ok"] is True
    assert summary["tests_fix_rounds"] == 5
    assert failures[0]["max_attempts"] == 4 and failures[-1]["max_attempts"] > 4
    assert all(e["max_attempts"] >= e["round"] for e in failures), "accumulated budget, not remaining steps"


@pytest.mark.parametrize("limit", [0, 1, 3])
@pytest.mark.parametrize("unattended", [False, True])
def test_explicit_nonnegative_limits_keep_exact_behavior(monkeypatch, tmp_path, limit, unattended):
    summary, checks, failures, _ = _exercise(monkeypatch, tmp_path, configured=limit, unattended=unattended)
    assert len(checks) == limit + 1 and summary["tests_fix_rounds"] == limit
    assert summary["tests"]["ok"] is False and summary["stop_reason"] == "complete_unverified"
    assert all(e["max_attempts"] == limit for e in failures)


@pytest.mark.parametrize("unattended", [False, True])
@pytest.mark.parametrize("configured", [-1, 3])
def test_stop_still_cancels_repair(monkeypatch, tmp_path, unattended, configured):
    summary, checks, _, _ = _exercise(monkeypatch, tmp_path, configured=configured, unattended=unattended, stop=True)
    assert len(checks) == 2 and summary["stop_reason"] == "cancelled"


def test_auto_does_not_recheck_without_a_new_mutation(monkeypatch, tmp_path):
    summary, checks, _, _ = _exercise(monkeypatch, tmp_path, no_edit=True)
    assert len(checks) == 1 and summary["tests"]["ok"] is False
    assert len(summary["tests_history"]) == 1 and summary["stop_reason"] == "complete_unverified"


def test_unexecuted_checker_does_not_create_evidence(monkeypatch, tmp_path):
    summary, checks, failures, _ = _exercise(monkeypatch, tmp_path, not_run=True)
    assert len(checks) == 1 and not summary["tests"]["ran"]
    assert summary["tests_history"] == [] and not failures
