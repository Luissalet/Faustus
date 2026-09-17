"""Processes screen (control center): `studio/checks/process-center.check.mjs`
asserts the adapter (`fetchProcesses`/`stopProcess`/`stopPort`), the screen's
sections and Stop flows (inline confirm, Ollama's `allow_protected`), and
the route wiring in AppShell/routes.ts, against the real source. Needs
node — skipped otherwise."""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "process-center.check.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node needed")


def test_process_center_check_passes():
    proc = subprocess.run(["node", str(_CHECK)], capture_output=True, text=True,
                          encoding="utf-8", cwd=str(_REPO), timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
