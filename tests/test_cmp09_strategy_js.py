"""tests/test_cmp09_strategy_js.py — CMP-09/CMP-12 (W2-F, CONTRATO_CMP_W2.md).

Static source-inspection check for the Studio half of this lot
(`studio/checks/strategy.check.mjs`) — JSX wiring inside Composer.tsx is
not pure logic a bundled import can exercise on its own, same reasoning as
tests/test_l99_studio_topology_js.py.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "strategy.check.mjs"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node needed")
def test_strategy_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok strategy" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_files_this_lot_owns_exist():
    for rel in (
        "studio/src/adapters/strategy.ts",
        "src/strategy_policy.py",
        "src/recipes.py",
        "routes/strategy_routes.py",
        "docs/api/strategy.md",
        "docs/recipes/review-changes.json",
        "docs/recipes/sources-to-report.json",
        "docs/recipes/design-function-and-tests.json",
        "docs/recipes/edit-passage-keep-tone.json",
    ):
        assert (_REPO / rel).exists(), f"missing {rel}"
