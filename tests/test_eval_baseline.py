"""Fast, no-app checks for `scripts/eval_run.py` and `tests/eval/baseline.json`
(the live comparisons live in `tests/eval/test_baseline_match.py`, which
needs the app fixture)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO / "tests" / "eval" / "baseline.json"


def test_baseline_file_is_well_formed_json_with_one_row_per_task():
    from tests.eval import tasks as T
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    names = {row["task"] for row in data["tasks"]}
    assert names == {t.name for t in T.ALL_TASKS}
    for row in data["tasks"]:
        assert row["ok"] is True
        assert row["rounds"] >= 1
        assert row["tool_calls"] >= 1


def test_compare_with_baseline_flags_a_task_that_regressed():
    import scripts.eval_run as eval_run

    result = {"tasks": [{"task": "bug_fix", "ok": False, "rounds": 3, "tool_calls": 2}]}
    cmp = eval_run.compare_with_baseline(result, BASELINE_PATH)
    assert cmp["compared"] is True
    assert cmp["regressed"] == ["bug_fix"]


def test_compare_with_baseline_is_clean_when_nothing_changed():
    import scripts.eval_run as eval_run

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    cmp = eval_run.compare_with_baseline(baseline, BASELINE_PATH)
    assert cmp["compared"] is True and cmp["regressed"] == []


def test_run_live_is_wired_but_not_exercised():
    """`--live` needs a real model endpoint this environment does not have
    (see the lot report's Limitaciones) — this only proves the CLI contract
    (required flags) is real, without starting anything."""
    import scripts.eval_run as eval_run

    with pytest.raises(SystemExit):
        eval_run.main(["--live"])  # missing --endpoint-url/--model
