"""The serve command the Cookbook builds (studio/src/lib/cookbook/serve.ts).

CPU-only runs that must not carry GPU flags, `python` rather than `python3`
on Windows, the native llama-server locally against the python module
remotely, an empty swap-space that must not become an empty flag, the Gemma 4
thinking chat template, the scanned vision projector, which engines a target
can actually serve, quoting and ports.

These check behaviour: `studio/checks/serve.check.mjs` builds commands and
reads them, rather than grepping the source for substrings — the way the
interface this replaced was pinned, which passed happily whenever the string
moved and the behaviour changed. Needs node and the repo's node_modules
(esbuild bundles the TS).
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "serve.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_studio_serve_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
