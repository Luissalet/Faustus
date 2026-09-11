"""tests/test_cmp13_alternatives_js.py — W2-G (CMP-13, CONTRATO_CMP_W2.md).

Static-source-inspection half of Alternatives (`studio/checks/
alternatives.check.mjs`) — the screen wiring, the adapter's error shape, and
the `/alternatives` route/sidebar/deep-link registration are JSX/router
wiring across several files, not pure logic a bundled import can exercise
directly, the same reasoning `tests/test_l99_studio_topology_js.py` gives
for its own check.

Run: python3 -m pytest tests/test_cmp13_alternatives_js.py -q -p no:cacheprovider -W ignore
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "alternatives.check.mjs"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node needed")
def test_alternatives_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok alternatives" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_files_this_lot_owns_exist():
    for rel in (
        "studio/src/adapters/alternatives.ts",
        "studio/src/screens/alternatives/AlternativesScreen.tsx",
        "studio/src/screens/alternatives/CompareView.tsx",
        "studio/src/screens/alternatives/alternatives.css",
        "src/alternatives.py",
        "routes/alternatives_routes.py",
        "src/agent_tools/alternatives_tools.py",
        "docs/api/alternatives.md",
        "docs/adaptations/decisions/CMP-13.md",
    ):
        assert (_REPO / rel).exists(), f"missing {rel}"


def test_no_shell_true_and_no_hardcoded_prices():
    """Two contract-wide rules this lot touches directly: `run_tests` spawns
    a real subprocess (never a shell), and cost is never a made-up number."""
    src = (_REPO / "src" / "alternatives.py").read_text(encoding="utf-8")
    assert "shell=True" not in src, "alternatives.py must never run a shell"
    assert '"unknown"' in src or "'unknown'" in src, "unknown cost must stay the literal string 'unknown', never 0"
