"""Render the agent loop panel: practices chosen by the kind of work, readable steps and the ring."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_OK = shutil.which("node") is not None and (ROOT / "node_modules" / "happy-dom").exists()


@pytest.mark.skipif(not _OK, reason="node + node_modules/happy-dom needed")
def test_agent_loop_check():
    result = subprocess.run(
        ["node", "studio/checks/agent-loop.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Agent loop:" in result.stdout
