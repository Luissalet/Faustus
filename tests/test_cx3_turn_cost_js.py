"""Runs studio/checks/turn-cost.check.mjs: the per-turn cost view keeps unknown
values unknown, the reply carries the run id that keys it, a "send after"
message is acknowledged only after it was handed over, and the new strings have
Spanish rows."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_OK = shutil.which("node") is not None and (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not _OK, reason="node + node_modules/esbuild needed")
def test_turn_cost_check():
    result = subprocess.run(
        ["node", "studio/checks/turn-cost.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok turn-cost" in result.stdout
