"""Lote 28 - ACT-06: loading / empty / error / (denied, incompatible).

Before this lote `EmptyState` had no vocabulary for "no permission" or "the
data on screen is out of date" — every caller that needed one of those
either mislabelled it `tone="error"` (which announces "retry will fix this",
false for a permission denial) or wrote its own markup. This runs the guard
at studio/checks/screen-states.check.mjs, which renders every `EmptyState`
tone through `react-dom/server` and statically checks that Activity.tsx (the
ACT-01 screen this lote owns) actually reaches all four states — the fourth
as the "last known activity, refresh before taking action" gate that
disables every mutating button in its detail pane, since that pane sits
beside a live list rather than replacing the whole screen.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK = REPO_ROOT / "studio" / "checks" / "screen-states.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (REPO_ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_screen_states_check_passes():
    proc = subprocess.run(
        ["node", str(CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(REPO_ROOT), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_empty_state_declares_the_four_tones():
    """A type-level regression guard independent of node/esbuild: if a tone
    is ever renamed or dropped, this fails even without a JS toolchain."""
    source = (REPO_ROOT / "studio" / "src" / "components" / "EmptyState.tsx").read_text(encoding="utf-8")
    assert "'empty' | 'error' | 'denied' | 'incompatible'" in source


def test_activity_gates_every_mutating_action_on_staleness():
    activity = (REPO_ROOT / "studio" / "src" / "screens" / "Activity.tsx").read_text(encoding="utf-8")
    gates = activity.count("disabled={currentStale")
    assert gates >= 8, f"expected the detail pane's actions to be gated on staleness, found {gates}"
