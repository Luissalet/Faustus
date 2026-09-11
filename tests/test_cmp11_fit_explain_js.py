"""tests/test_cmp11_fit_explain_js.py — CMP-11 (INFORME V2 §3.10), Studio
half: the capability-fit badges in the model picker
(studio/src/adapters/fit.ts, studio/src/screens/ModelPalette.tsx,
studio/src/shell/palette.css).

This wiring is JSX reading a live fetch, not pure logic a bundled import
can exercise — so, like tests/test_l99_studio_topology_js.py, it is checked
by source inspection (studio/checks/fit_explain.check.mjs) rather than
a DOM render. No network calls anywhere in this file.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "fit_explain.check.mjs"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node needed")
def test_cmp11_fit_explain_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok fit_explain" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_files_this_lot_touches_exist():
    for rel in (
        "studio/src/adapters/fit.ts",
        "studio/src/screens/ModelPicker.tsx",
        "studio/src/screens/ModelPalette.tsx",
        "studio/src/shell/palette.css",
        "src/model_capabilities.py",
        "src/provider_policy.py",
        "routes/model_routes.py",
        "docs/api/model_capabilities.md",
        "docs/adaptations/decisions/CMP-11.md",
    ):
        assert (_REPO / rel).exists(), f"missing {rel}"
