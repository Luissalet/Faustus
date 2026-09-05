"""The drawing surface of a note (studio/src/lib/paint.ts).

Where a finger lands on a canvas that is displayed smaller than it is, the
bounded undo stack, the `bg:<url>` sentinel a note uses to carry a
background picture in its colour field, and which URLs are allowed to be a
picture at all. `studio/checks/paint.check.mjs` drives them and prints
ok/FAIL lines; this test runs it. Needs node and the repo's node_modules
(esbuild bundles the TS).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "paint.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_studio_paint_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
