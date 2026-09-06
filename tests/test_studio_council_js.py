"""The Council screen's reasoning.

A council room is where several models think about one matter and exactly one
of them may act, so everything the screen claims about it is derived in
`studio/src/adapters/council.ts` rather than inside a component: the transcript
grouped into turns, whether a room is blocked and — separately — whether
anything open forbids the word `verified`, where a reconnection resumes without
repeating an event or hiding a hole, a verdict read to its label, and who is
holding a resource when two rows spell one path differently.

A panel whose reasoning cannot be checked is decoration, so
`studio/checks/council.check.mjs` drives those functions and prints ok/FAIL
lines; this test runs it. Needs node and the repo's node_modules (esbuild
bundles the TS).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "council.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_studio_council_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
