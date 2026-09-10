"""Lote 49b (P1) - A11Y-02: streaming announced in grouped batches, not per
token (studio/src/adapters/streamAnnounce.ts, wired into Transcript.tsx's
`useGroupedStreamAnnouncement` behind a `polite` live region).

`studio/checks/p1-a11y02-stream-announce.check.mjs` drives the pure gating
function and prints ok/FAIL lines; this test runs it. Needs node and the
repo's node_modules (esbuild bundles the TS) — same setup
`test_studio_panel_js.py` uses.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "p1-a11y02-stream-announce.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_p1_a11y02_stream_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
