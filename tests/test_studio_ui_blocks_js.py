"""Runs studio/checks/ui-blocks.check.mjs (reply blocks: validator + wiring)."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None or not (ROOT / "node_modules" / "esbuild").exists(),
                    reason="node and esbuild are needed")
def test_ui_blocks_check():
    r = subprocess.run(["node", str(ROOT / "studio" / "checks" / "ui-blocks.check.mjs")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]


def test_the_agent_is_told_about_reply_blocks():
    from src import agent_loop
    text = Path(agent_loop.__file__).read_text(encoding="utf-8")
    assert text.count("```choices``` block") == 2


@pytest.mark.skipif(shutil.which("node") is None or not (ROOT / "node_modules" / "esbuild").exists(),
                    reason="node and esbuild are needed")
def test_prose_lint_check():
    r = subprocess.run(["node", str(ROOT / "studio" / "checks" / "prose-lint.check.mjs")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
