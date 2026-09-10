"""EVAL-01/BENCH-02 "measure": the live scripted run's numbers must match
`tests/eval/baseline.json` — the same comparison `scripts/eval_run.py --live`
makes against a real model, exercised here against the recorded one so it is
provable without a live endpoint.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.eval import tasks as T

BASELINE = json.loads((Path(__file__).parent / "baseline.json").read_text(encoding="utf-8"))
BASELINE_BY_NAME = {t["task"]: t for t in BASELINE["tasks"]}


def test_baseline_covers_every_registered_task():
    assert set(BASELINE_BY_NAME) == {t.name for t in T.ALL_TASKS}


@pytest.mark.parametrize("task", T.ALL_TASKS, ids=[t.name for t in T.ALL_TASKS])
def test_scripted_run_matches_the_saved_baseline(eval_app, tmp_path, task):
    base = BASELINE_BY_NAME[task.name]
    ws = tmp_path / task.name
    ws.mkdir()
    task.setup(ws)
    eval_app.script(task.script)
    session_id = eval_app.new_session(f"eval-baseline-{task.name}")
    result = eval_app.send_turn(session_id, task.message, workspace=str(ws))
    outcome = task.verify(ws, result)

    assert outcome["ok"] == base["ok"]
    assert result.rounds == base["rounds"]
    assert len(result.tool_calls) == base["tool_calls"]
    assert list(result.tools_used()) == base["tools_used"]
    # total_tokens is deterministic here: fake_llm derives completion_tokens
    # from the fixed script text's length, so it is stable across runs — the
    # one metric worth pinning exactly against a recorded baseline.
    assert result.metrics.get("total_tokens") == base["total_tokens"]
