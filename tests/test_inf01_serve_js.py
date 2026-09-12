"""INF-01 — Cookbook/serve veracity (H01-H04). Runs
`studio/checks/serve_veracity.check.mjs`, which drives the real
`studio/src/lib/cookbook/serve.ts` through esbuild rather than
re-implementing its logic in Python — same pattern
`tests/test_l89_git_merge_js.py`/`tests/test_l65_studio_events_js.py` use
for their file pairs.

Covers: architecture-gated MoE/MTP (never from a name substring, T01/T02),
verified MoE really produces the branch, MTP only with `mtp: true` (A17B
included — no substring list), no unmeasured speedup claim (H02), and the
native-vs-`llama_cpp.server` translation receipt (T03: `implementation` and
`omitted` with a reason, never a silent drop).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (ROOT / "node_modules" / "esbuild" / "lib" / "main.js").exists()
_SKIP = pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")


@_SKIP
def test_serve_veracity_js():
    result = subprocess.run(
        ["node", "studio/checks/serve_veracity.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout
