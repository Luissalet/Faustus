"""Lote 39 / PERF-01/UX-05: `frameBatcher` (studio/src/lib/frame-batch.ts)
coalesces rapid values into at most one delivery per animation frame —
the mechanism `Transcript.tsx`'s `useFrameBatched` wraps so a streaming
turn repaints at most 60 times a second instead of once per delta, which
is what let the composer stop taking keystrokes during a fast stream.

Runs studio/checks/l39-frame-batch.check.mjs, which drives the real module
(not a re-implementation) with a synchronous fake scheduler.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")


def test_frame_batcher_coalesces_to_one_delivery_per_frame():
    result = subprocess.run(
        ["node", "studio/checks/l39-frame-batch.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL OK" in result.stdout
    assert "FAIL" not in result.stdout
