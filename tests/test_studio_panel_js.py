"""The side panel: what the agent sees while it works.

The raster whitelist a frame has to pass (an SVG or an HTML data URL is a
script, not a screenshot), the bounded frame list, the panel opening itself
once per turn rather than every time, the live marker, and desktop
screenshots sharing the panel labelled as themselves.

`studio/checks/panel.check.mjs` drives the reducer and prints ok/FAIL lines;
this test runs it. Needs node and the repo's node_modules (esbuild bundles
the TS).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "panel.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_studio_panel_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
