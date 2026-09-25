"""Lot T: runs `studio/checks/think-mode.check.mjs` -- the composer's
reasoning-mode chip, its per-chat persistence, the `think_mode` SSE decode
and the `/think auto|fast|think|deep` wiring. Same node-subprocess pattern
as tests/test_cmp_gen_panel_js.py."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "think-mode.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild").exists()


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + esbuild needed")
def test_think_mode_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok think_mode" in proc.stdout
