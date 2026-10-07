"""Studio tool card: a long tool output is never cut in silence (the card
used to paint the first 6,000 characters of a 32,678-character inspection
with nothing saying so), and the "back to the bottom" pill only says "New
messages" when some arrived. Runs `studio/checks/tool-output-preview.check.mjs`,
which drives the real Transcript.tsx helpers through esbuild - same pattern
as tests/test_l65_studio_events_js.py.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_tool_output_preview_and_new_messages_pill():
    result = subprocess.run(
        ["node", "studio/checks/tool-output-preview.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok tool-output-preview" in result.stdout
