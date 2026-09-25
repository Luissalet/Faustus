"""Studio: cited tool-result ids render as footnotes (studio/checks/cite-tool-results.check.mjs)."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_cited_results_become_footnotes():
    proc = subprocess.run(["node", "studio/checks/cite-tool-results.check.mjs"], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "cite-tool-results: ok" in proc.stdout
