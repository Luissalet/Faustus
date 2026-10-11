"""OBJ-60 Studio: fact change-chain adapter + Brain entity panel wiring."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (ROOT / "node_modules" / "esbuild").is_dir()


@pytest.mark.skipif(not _HAS_NODE, reason="node not on PATH")
@pytest.mark.skipif(not _HAS_ESBUILD, reason="studio node_modules/esbuild missing")
def test_brain_fact_history_adapter_and_panel_wiring():
    check = ROOT / "studio" / "checks" / "brain-fact-history.check.mjs"
    assert check.is_file(), check
    proc = subprocess.run(
        ["node", str(check)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
