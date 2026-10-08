"""Comparing two alternatives in Studio (OBJ-47, studio/src/lib/altpair.ts).

Reading the server's unified diff into numbered rows, laying it out side by
side, filtering and moving through the file list with the keyboard, choosing
the pair, and the wiring of the screen (adapter call, Spanish rows for every
string, tokens only, the diff viewer as the one thing that scrolls sideways).
`studio/checks/alternatives_pair.check.mjs` drives them and prints ok/FAIL
lines; this test runs it. Needs node and the repo's node_modules (esbuild
bundles the TS).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "alternatives_pair.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_studio_alternatives_pair_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
