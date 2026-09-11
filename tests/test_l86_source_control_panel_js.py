"""Lote 86 (OBJ-4, CONTRATO_GIT_4.md) — Studio: SourceControlPanel reusable,
the "Repositories" section in Project.tsx, the live compact panel inside the
project's chat, and the command-palette/`git_policy`-chip access points.

There is no DOM renderer set up in this repo's Node checks (see every other
`*.check.mjs` here), so — same posture `l65-source-wiring.check.mjs` and
`tests/test_l65_studio_events_js.py` already use for Studio.tsx/Transcript.tsx
wiring that a bundled pure-logic import cannot exercise — this is checked by
source inspection: every file this lot touches actually declares the props,
imports and JSX the contract asks for, and the pieces moved out of
SourceControl.tsx (now a thin wrapper) really did move rather than being
duplicated.

Run node studio/checks/l86-source-control-panel.check.mjs by hand to see the
same checks with their individual pass/fail lines.
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None
_SKIP = pytest.mark.skipif(not _HAS_NODE, reason="node needed")


@_SKIP
def test_l86_source_control_panel_wiring():
    result = subprocess.run(
        ["node", "studio/checks/l86-source-control-panel.check.mjs"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok l86-source-control-panel" in result.stdout
