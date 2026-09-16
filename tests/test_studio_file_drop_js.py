"""Composer file-drop highlight: depth counter + overlay wiring.

`studio/checks/file-drop.check.mjs` drives `studio/src/lib/file-drop.ts` and
inspects Composer.tsx / studio.css so the chat box cannot flicker (and miss
the drop) when a file is hovered across its children.

Needs node and the repo's node_modules (esbuild bundles the TS) — skipped
otherwise, same guard as the existing JS checks.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "file-drop.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_file_drop_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
