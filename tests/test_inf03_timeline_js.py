"""INF-03 (Lote B — Studio) — the "why did it take this long?" timeline.
Runs `studio/checks/execution_timeline.check.mjs`, which drives the real
`studio/src/adapters/chat.ts` (`executionMetricsFrom`, `timelineBars`)
through esbuild rather than re-implementing their logic in Python — same
pattern `tests/test_inf01_serve_js.py`/`tests/test_inf02_receipt_js.py` use.

Covers the one rule CONTRATO_INF03.md answers to: a phase that was not
observed (`absent`) never produces a bar, not even a zero-length one
(matches `docs/api/execution_metrics.md`'s worked example and its T07 case);
a malformed/half-parsed `MetricValue` off the wire degrades to a true
absent; phases that overlap the total (sum past `total_ms`, including a
single phase over 100% on its own) are flagged via `overlap` and keep their
true, unrescaled widths rather than being shrunk to fit.
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
def test_execution_timeline_js():
    result = subprocess.run(
        ["node", "studio/checks/execution_timeline.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout
