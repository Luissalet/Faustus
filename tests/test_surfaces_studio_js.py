"""The whiteboard's frontend logic (studio/src/lib/pizarraModel.ts) and the wiring that lets the person and the
agent share a board, checked under plain node (no browser): patches and undo, geometry, the pen, and the places
the surface is registered."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "surfaces.check.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node needed")
def test_pizarra_model_checks_pass():
    proc = subprocess.run(["node", str(_CHECK)], capture_output=True, text=True, encoding="utf-8", cwd=str(_REPO), timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok pizarra" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_model_is_pure_logic():
    text = (_REPO / "studio" / "src" / "lib" / "pizarraModel.ts").read_text(encoding="utf-8")
    for banned in ("document.", "window.", "fetch(", "from 'react'", "useState("):
        assert banned not in text, f"pizarraModel.ts should stay pure logic (found {banned!r})"


def test_no_direct_fetch_in_the_screen():
    for rel in ("studio/src/screens/studio/Pizarra.tsx", "studio/src/screens/studio/SurfaceHome.tsx"):
        text = (_REPO / rel).read_text(encoding="utf-8")
        assert "fetch(" not in text.replace("await (await fetch(boardSvgUrl", ""), f"{rel} must go through adapters/surfaces.ts"


def test_panel_and_command_wiring():
    panel = (_REPO / "studio" / "src" / "screens" / "studio" / "panel.ts").read_text(encoding="utf-8")
    assert "'pizarra'" in panel and "isWideTab" in panel and "type:'surface'" in panel
    side = (_REPO / "studio" / "src" / "screens" / "studio" / "SidePanel.tsx").read_text(encoding="utf-8")
    assert "<Pizarra" in side and "SurfaceHome" in side
    commands = (_REPO / "studio" / "src" / "screens" / "studio" / "commands.ts").read_text(encoding="utf-8")
    assert "name: 'pizarra'" in commands
    studio = (_REPO / "studio" / "src" / "screens" / "Studio.tsx").read_text(encoding="utf-8")
    assert "useSurfaceEvents(" in studio and "case 'pizarra'" in studio
