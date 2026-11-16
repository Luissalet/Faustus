"""Studio side of the brain review, ratify and galaxy features: the adapters
and the galaxy camera maths run in Node; the screens are checked by source."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None and (_REPO / "node_modules" / "esbuild").exists()


@pytest.mark.skipif(not _HAS_NODE, reason="node and node_modules/esbuild needed")
def test_brain_review_checks_pass():
    proc = subprocess.run(
        ["node", str(_REPO / "studio" / "checks" / "brain-review.check.mjs")],
        capture_output=True, text=True, encoding="utf-8", cwd=str(_REPO), timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok brain-review" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_brain_screen_wires_the_new_views():
    screen = (_REPO / "studio/src/screens/Brain.tsx").read_text(encoding="utf-8")
    for needle in ("GalaxyView", "ReviewPanel", "brain-view-galaxy", "brain-view-review", "loadReview"):
        assert needle in screen, needle
    for rel in ("studio/src/screens/brain/GalaxyView.tsx", "studio/src/screens/brain/ReviewPanel.tsx",
                "studio/src/screens/brain/galaxyMath.ts", "studio/src/adapters/brainReview.ts"):
        assert (_REPO / rel).exists(), rel


def test_screens_reach_the_server_only_through_the_adapter():
    for rel in ("GalaxyView.tsx", "ReviewPanel.tsx"):
        text = (_REPO / "studio/src/screens/brain" / rel).read_text(encoding="utf-8")
        assert "fetch(" not in text, rel
