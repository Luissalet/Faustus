"""A scheduled shell task is its future command: the guard judges it like bash.

Security audit 26-09: `manage_tasks` create with `task_type=action` and
`action_name=run_script` stores `prompt` as a shell script the scheduler runs
later (src/builtin_actions.py, `bash -c`). In a clean run that went through
with no card, so `rm -rf ~` on a timer skipped the destructive-command guard
that the same line in `bash` would hit.
"""
import json

import pytest

import src.tool_capabilities as tc


@pytest.fixture(autouse=True)
def _enforce(monkeypatch):
    monkeypatch.setattr(tc, "get_setting", lambda key, default=None: {
        "agent_command_guard_mode": "enforce", "agent_command_guard_packs": "all"}.get(key, default))
    tc._reset_command_guard_cache()
    yield
    tc._reset_command_guard_cache()


def _task(**kw):
    base = {"action": "create", "name": "t", "task_type": "action", "schedule": "daily", "scheduled_time": "09:00"}
    base.update(kw)
    return json.dumps(base)


def test_a_destructive_scheduled_script_is_carded_like_bash():
    bash = tc._command_guard_denial("bash", "rm -rf ~")
    assert bash is not None
    task = tc._command_guard_denial("manage_tasks", _task(action_name="run_script", prompt="rm -rf ~"))
    assert task is not None
    assert tc.command_guard_requires_approval("manage_tasks", _task(action_name="ssh_command", prompt="rm -rf /"))


def test_harmless_or_non_shell_tasks_pass():
    assert tc._command_guard_denial("manage_tasks", _task(action_name="run_script", prompt="echo hola")) is None
    assert tc._command_guard_denial("manage_tasks", _task(action_name="weather_report", prompt="rm -rf ~")) is None
    assert tc._command_guard_denial("manage_tasks", json.dumps({"action": "list"})) is None
    assert tc._command_guard_denial("manage_tasks", "not json") is None


def test_editing_the_prompt_of_a_stored_shell_task_is_judged_too(monkeypatch):
    monkeypatch.setattr(tc, "_task_action_of", lambda task_id: "run_local" if task_id == "t1" else None)
    edit = json.dumps({"action": "edit", "task_id": "t1", "prompt": "rm -rf ~"})
    assert tc._command_guard_denial("manage_tasks", edit) is not None
    other = json.dumps({"action": "edit", "task_id": "t2", "prompt": "rm -rf ~"})
    assert tc._command_guard_denial("manage_tasks", other) is None


def test_the_gate_denies_it_in_a_clean_run():
    ctx = tc.ToolRunSecurityContext()
    decision = ctx.decision_for("manage_tasks", _task(action_name="run_script", prompt="rm -rf ~"))
    assert decision.allowed is False
