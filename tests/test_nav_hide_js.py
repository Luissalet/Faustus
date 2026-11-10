"""Runs studio/checks/nav-hide.check.mjs: the sidebar can be put away and comes
back as it stood, the choice is remembered, and the button, the shortcut, the
palette and the agent's switch are all wired to the same state."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_OK = shutil.which("node") is not None and (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not _OK, reason="node + node_modules/esbuild needed")
def test_nav_hide_check():
    result = subprocess.run(
        ["node", "studio/checks/nav-hide.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok nav-hide" in result.stdout
