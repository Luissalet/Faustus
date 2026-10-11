"""Changes tab of Memory (#242): wording in Spanish, grouping per turn, the
«Vaciar historial» wiring and the transcript link by turn.

`tests/memory_changes.check.mjs` bundles the tab's model with the repo's
esbuild. Needs node and `node_modules/esbuild`.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "tests" / "memory_changes.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_memory_changes_ui_checks_pass():
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
