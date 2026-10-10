"""Period-budget meter in Studio Vitals (Agora #231).

`tests/period_budget_vitals.check.mjs` bundles `periodBudget.ts`, checks the
parse/level helpers, and asserts Vitals + OBJETIVOS wiring. Lives under
`tests/` (not `studio/checks/`) so it does not fight the studio/checks lock
held by another task. Needs node and the repo's esbuild.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "tests" / "period_budget_vitals.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_studio_period_budget_vitals_checks_pass():
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


def test_vitals_wires_period_budget_adapter_and_honest_empties():
    vitals = (_REPO / "studio" / "src" / "screens" / "studio" / "Vitals.tsx").read_text(
        encoding="utf-8"
    )
    adapter = (_REPO / "studio" / "src" / "adapters" / "periodBudget.ts").read_text(
        encoding="utf-8"
    )
    assert "periodBudget" in vitals and "<PeriodBudgetSection />" in vitals
    assert "No period target set: nothing is paced" in vitals
    assert "Could not read the period budget: {why}" in vitals
    assert "/api/budget/period" in adapter
    assert "parsePeriodBudget" in adapter
