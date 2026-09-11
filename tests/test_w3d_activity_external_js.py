"""tests/test_w3d_activity_external_js.py — W3-D (CONTRATO_W3.md).

Static-source-inspection half of the "Externos" section/filter
(`studio/checks/activity_external.check.mjs`) — JSX/adapter wiring across
`Activity.tsx`/`adapters/activity.ts`/`activity.css`, the same reasoning
`tests/test_l99_studio_topology_js.py` gives for its own check.

Run: python3 -m pytest tests/test_w3d_activity_external_js.py -q -p no:cacheprovider -W ignore
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "activity_external.check.mjs"
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node needed")
def test_activity_external_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok activity_external" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_files_this_lot_owns_exist():
    for rel in (
        "studio/src/adapters/activity.ts",
        "studio/src/screens/Activity.tsx",
        "studio/src/screens/activity.css",
        "studio/checks/activity_external.check.mjs",
        "src/branching_futures/isolation.py",
        "src/branching_futures/service.py",
        "src/alternatives.py",
        "src/desktop_control_session.py",
        "docs/api/alternatives.md",
        "docs/api/external_runtimes.md",
    ):
        assert (_REPO / rel).exists(), f"missing {rel}"
