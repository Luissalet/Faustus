"""Runs studio/checks/agora-notify.check.mjs: what waits for the person in the Agora (a question put to them, a message
that names them) becomes a tray notification once, with the place to open, and is settled when it no longer waits."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_OK = shutil.which("node") is not None and (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not _OK, reason="node + node_modules/esbuild needed")
def test_agora_notify_check():
    result = subprocess.run(
        ["node", "studio/checks/agora-notify.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok agora-notify" in result.stdout
