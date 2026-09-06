"""The State Mirror screen's reasoning.

The mirror says what is true right now, when it was last looked at and how it
is known, so everything the screen claims about that is derived in
`studio/src/adapters/stateMirror.ts` rather than inside a component: the four
freshness words and the order they rank in, whether a value may be drawn as
the CURRENT state (only `fresh` ever may), which of the five questions a row
answers and that a live grouping is never reached through a value we may not
trust, an entity id surviving a round trip through a URL, and where a
reconnection resumes without repeating an event or moving backwards.

A panel whose reasoning cannot be checked is decoration, so
`studio/checks/stateMirror.check.mjs` drives those functions and prints
ok/FAIL lines; this test runs it. Needs node and the repo's node_modules
(esbuild bundles the TS).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "stateMirror.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_studio_state_mirror_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
