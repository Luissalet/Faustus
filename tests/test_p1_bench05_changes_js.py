"""Lote 49b (P1) - BENCH-05: "ChangeSet actual con diff por fichero" in the
workbench's side panel (studio/src/adapters/workbenchChanges.ts, wired into
SidePanel.tsx's PlanAndChanges).

`studio/checks/p1-bench05-changes.check.mjs` drives the pure aggregator and
prints ok/FAIL lines; this test runs it.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "p1-bench05-changes.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_p1_bench05_changes_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_plan_and_changes_is_wired_into_the_side_panel():
    side_panel = (_REPO / "studio" / "src" / "screens" / "studio" / "SidePanel.tsx").read_text(encoding="utf-8")
    assert "aggregateFileChanges" in side_panel
    assert "<PlanAndChanges turns={turns}/>" in side_panel
