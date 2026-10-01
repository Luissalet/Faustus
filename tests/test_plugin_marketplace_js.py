"""Runs the Studio checks for the Hoard marketplace screen and Prospero's image
settings. Neither was run by the suite, and the marketplace preview had fallen
behind the catalogue (a newly listed Hoard broke it); its manifests now come
from the catalogue rows."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_OK = shutil.which("node") is not None and (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not _OK, reason="node + node_modules/esbuild needed")
@pytest.mark.parametrize("check", ["plugin-marketplace", "prospero-image-settings"])
def test_studio_check(check):
    result = subprocess.run(
        ["node", f"studio/checks/{check}.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL OK" in result.stdout
