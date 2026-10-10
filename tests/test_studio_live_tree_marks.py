"""Workspace tree lit up with what the turn read / wrote (#247).

`tests/live_tree_marks.check.mjs` bundles `file-activity.ts` and checks the pure
event -> {path: read|written} functions plus the Files tab wiring and the Spanish
rows. Needs node and the repo's esbuild.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "tests" / "live_tree_marks.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_live_tree_marks_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(_REPO),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
