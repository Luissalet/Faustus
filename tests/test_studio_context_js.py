"""The Context Engine screen's arithmetic.

Everything the panel claims about the engine is derived in
`studio/src/adapters/context.ts` rather than inside a component: the
diagnostics body normalised (including the branches where the server answers
`{"error": ...}` because a diagnostic may never raise), omissions grouped by
reason, tokens per section as a share of the packet, a verdict read to its
label, and the repo's refusal convention -- 200 with
`{"ok": false, "error": {"path", "message"}}`.

A dashboard whose numbers cannot be checked is decoration, so
`studio/checks/context.check.mjs` drives those functions and prints ok/FAIL
lines; this test runs it. Needs node and the repo's node_modules (esbuild
bundles the TS).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "context.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_studio_context_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
