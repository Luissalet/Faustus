"""L29 (integrates L28's ACT-03 note): GET /api/questions feeds
`loadActivity()` as `kind: 'question'` rows, and `answerQuestion()` drives
the same `sendTurn`/`questionId` path the live AskCard uses instead of a
second resolution mechanism.

Runs studio/checks/l29-activity-questions.check.mjs, mirroring
tests/test_studio_activity_js.py's own pattern for adapters/activity.ts.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_open_questions_reach_the_activity_feed_and_can_be_answered():
    result = subprocess.run(
        ["node", "studio/checks/l29-activity-questions.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL OK" in result.stdout


def test_activity_screen_declares_the_question_kind_and_tray():
    source = (ROOT / "studio" / "src" / "screens" / "Activity.tsx").read_text(encoding="utf-8")
    assert "'question'" in source
    assert "QuestionAnswerPanel" in source
    assert "answerQuestion" in source
