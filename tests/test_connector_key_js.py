"""Runs studio/checks/connector-key.check.mjs: a server listed without a
connector record (id null) never opens its tools dialog on its own, and its
tools come from its MCP server."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_OK = shutil.which("node") is not None and (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not _OK, reason="node + node_modules/esbuild needed")
def test_connector_key_check():
    result = subprocess.run(
        ["node", "studio/checks/connector-key.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok connector-key" in result.stdout
