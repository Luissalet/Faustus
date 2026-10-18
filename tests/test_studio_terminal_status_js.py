"""Native terminal errors survive Studio history hydration without a replay."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(
    not shutil.which("node") or not (ROOT / "node_modules/esbuild/lib/main.js").exists(),
    reason="node and the worktree's npm dependencies are required",
)
def test_studio_terminal_status_runtime():
    result = subprocess.run(
        ["node", "studio/checks/studio-terminal-status.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL OK" in result.stdout
