"""Runs studio/checks/advisor-advice.check.mjs: the advisor's note reaches the
turn (live `advisor_advice` event and reloaded `metadata.advisor`) and the
collapsible card and its Spanish strings are wired."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_OK = shutil.which("node") is not None and (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not _OK, reason="node + node_modules/esbuild needed")
def test_advisor_advice_check():
    result = subprocess.run(
        ["node", "studio/checks/advisor-advice.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok advisor-advice" in result.stdout
