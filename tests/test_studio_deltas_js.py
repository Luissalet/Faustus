"""The Deltas screen's reasoning.

A delta says what changed between two revisions, whether it is what was asked
for, and how well we know. Everything the screen claims about that is derived
in `studio/src/adapters/deltas.ts` rather than inside a component: that
`preserved` is never drawn for a row nobody measured (not detected is not
preserved), that an `unknown` is never grouped or sorted in amongst the ones
that held, that coverage and confidence stay two numbers and are never
multiplied into one, that an unmeasured coverage axis reads as `null` and never
as zero, that a blocking finding comes back whole rather than as a count, and
that two findings are ordered here the same way the server orders them.

A panel whose reasoning cannot be checked is decoration, so
`studio/checks/deltas.check.mjs` drives those functions and prints ok/FAIL
lines; this test runs it. Needs node and the repo's node_modules (esbuild
bundles the TS).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "deltas.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_studio_deltas_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
