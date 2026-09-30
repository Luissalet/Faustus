"""Runs studio/checks/vitals-residents.check.mjs: the header gauge and its panel
list every model in memory with who loaded it, what it holds and on which
cards, also against an older server, and the new strings have Spanish rows."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_OK = shutil.which("node") is not None and (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not _OK, reason="node + node_modules/esbuild needed")
def test_vitals_residents_check():
    result = subprocess.run(
        ["node", "studio/checks/vitals-residents.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok vitals-residents" in result.stdout
