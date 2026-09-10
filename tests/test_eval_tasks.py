"""EVAL-01, fast half: `tests/eval/tasks.py`'s own logic, with no server and
no model — every `Task.setup`/`Task.verify` pair proven directly against a
hand-built workspace, so a broken fixture fails here in milliseconds instead
of only inside the ~5 s live-app suite (`tests/eval/test_representative_tasks.py`).
"""
from __future__ import annotations

import json
import re

from tests.eval import tasks as T


def test_every_task_is_registered_exactly_once():
    names = [t.name for t in T.ALL_TASKS]
    assert len(names) == len(set(names)) == 6
    assert set(names) == {"bug_fix", "feature", "refactor", "investigation", "document", "tabular"}


def test_every_scripted_response_that_looks_like_a_tool_call_is_valid_json():
    """A fixture with a typo'd JSON arg would fail deep inside a live turn,
    ~30s into the suite, for a reason that has nothing to do with the code
    under test. Catch it here instead."""
    fence_re = re.compile(r"^```([a-zA-Z_]+)\n(.*)\n```$", re.S)
    for task in T.ALL_TASKS:
        for step in task.script:
            m = fence_re.match(step.strip())
            if not m:
                continue  # a plain prose "final answer" step, not a tool call
            tool, body = m.group(1), m.group(2)
            parsed = json.loads(body)  # raises with a clear task name on failure
            assert isinstance(parsed, dict), f"{task.name}: {tool} args must be a JSON object"


def test_bug_fix_verify_fails_before_the_fix_and_passes_after(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    T.BUG_FIX.setup(ws)
    before = T.BUG_FIX.verify(ws, None)
    assert before["ok"] is False  # the bug is still there: add() subtracts
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    after = T.BUG_FIX.verify(ws, None)
    assert after["ok"] is True


def test_feature_verify_fails_before_the_feature_and_passes_after(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    T.FEATURE.setup(ws)
    before = T.FEATURE.verify(ws, None)
    assert before["ok"] is False  # multiply() does not exist yet
    (ws / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n", encoding="utf-8")
    after = T.FEATURE.verify(ws, None)
    assert after["ok"] is True


def test_refactor_verify_rejects_a_fix_that_does_not_reuse_the_helper(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    T.REFACTOR.setup(ws)
    before = T.REFACTOR.verify(ws, None)
    assert before["ok"] is False and before["reused_shared_helper"] is False
    # A "refactor" that just deletes the duplication without reusing the
    # shared helper still passes the tests, but must NOT count as done: this
    # is EVAL-01's "measure the real result", not "tests are green".
    (ws / "shapes.py").write_text(
        "def area_rectangle(w, h):\n    return w * h\n\n\ndef area_square(s):\n    return s ** 2\n",
        encoding="utf-8")
    sideways = T.REFACTOR.verify(ws, None)
    assert sideways["verification"]["ok"] is True and sideways["reused_shared_helper"] is False
    assert sideways["ok"] is False
    (ws / "shapes.py").write_text(
        "def area_rectangle(w, h):\n    return w * h\n\n\ndef area_square(s):\n    return area_rectangle(s, s)\n",
        encoding="utf-8")
    real_fix = T.REFACTOR.verify(ws, None)
    assert real_fix["ok"] is True


def test_investigation_verify_rejects_a_report_that_never_cites_the_figure(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    T.INVESTIGATION.setup(ws)
    before = T.INVESTIGATION.verify(ws, None)
    assert before["ok"] is False  # no report.md at all yet
    (ws / "report.md").write_text("# Informe\n\nNo cite ninguna cifra.\n", encoding="utf-8")
    uncited = T.INVESTIGATION.verify(ws, None)
    assert uncited["ok"] is False
    (ws / "report.md").write_text(
        "# Informe\n\n23 requisitos P0 estaban en estado existente sobre 100 auditados.\n", encoding="utf-8")
    cited = T.INVESTIGATION.verify(ws, None)
    assert cited["ok"] is True and cited["citation_verdict"] == "supported"


def test_document_verify_requires_all_three_sections(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    T.DOCUMENT.setup(ws)
    assert T.DOCUMENT.verify(ws, None)["ok"] is False
    (ws / "memo.md").write_text("# Purpose\nonly this one\n", encoding="utf-8")
    assert T.DOCUMENT.verify(ws, None)["ok"] is False
    (ws / "memo.md").write_text("# Purpose\nx\n\n# Steps\nx\n\n# Risks\nx\n", encoding="utf-8")
    assert T.DOCUMENT.verify(ws, None)["ok"] is True


def test_tabular_verify_recomputes_the_expected_sum_independently(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    T.TABULAR.setup(ws)
    assert T.TABULAR.verify(ws, None)["ok"] is False
    (ws / "total.txt").write_text("999", encoding="utf-8")
    assert T.TABULAR.verify(ws, None)["ok"] is False  # wrong number
    (ws / "total.txt").write_text(str(sum(a for _i, a in T._CSV_ROWS)), encoding="utf-8")
    assert T.TABULAR.verify(ws, None)["ok"] is True
