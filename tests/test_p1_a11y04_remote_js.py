"""Lote 49b (P1) - A11Y-04: detecting insecure remote access
(studio/src/adapters/remoteAccess.ts), wired into SidePanel.tsx as a warning
banner ("acceso remoto solo con auth + HTTPS o tunel, con aviso claro si
no").

`studio/checks/p1-a11y04-remote.check.mjs` drives the pure classifier and
prints ok/FAIL lines; this test runs it.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "p1-a11y04-remote.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_p1_a11y04_remote_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_warning_is_actually_wired_into_the_workbench():
    side_panel = (_REPO / "studio" / "src" / "screens" / "studio" / "SidePanel.tsx").read_text(encoding="utf-8")
    assert "isInsecureRemoteAccess(" in side_panel
    assert 'data-tone="warning"' in side_panel
