"""EVAL-01, live half: every task in `tasks.py` run through a REAL turn
(subprocess app + `tests/e2e/fake_llm`'s scripted, recorded model), with
tools offered/used, rounds and tokens read off the wire the same way Studio
does, and the result checked by the task's own `verify` — never by the
model's closing sentence.
"""
from __future__ import annotations

import pytest

from tests.eval import tasks as T


@pytest.mark.parametrize("task", T.ALL_TASKS, ids=[t.name for t in T.ALL_TASKS])
def test_representative_task(eval_app, tmp_path, task):
    ws = tmp_path / task.name
    ws.mkdir()
    task.setup(ws)

    eval_app.script(task.script)
    session_id = eval_app.new_session(f"eval-{task.name}")
    result = eval_app.send_turn(session_id, task.message, workspace=str(ws))

    outcome = task.verify(ws, result)
    assert outcome["ok"], f"{task.name}: {outcome.get('detail')}"

    # The measurements EVAL-01 asks for, always present regardless of the
    # verdict above — a fixture that passes on a 0-round turn is a fixture
    # that never actually asked the model anything.
    assert result.rounds >= 1
    assert result.tool_calls, f"{task.name}: no tool ran a real turn should have used at least one"
    used = set(result.tools_used())
    # Every tool the script actually invoked left a `tool_start` on the wire —
    # the offered/used distinction EVAL-01 names is only meaningful once this
    # holds: "used" must be a real subset of what ran, not a guess.
    assert used <= set(result.tools_offered) or not result.tools_offered, (
        f"{task.name}: tool(s) {used - set(result.tools_offered)} ran without ever being offered")
