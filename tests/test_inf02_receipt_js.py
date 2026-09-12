"""INF-02 (Lote B — Studio) — capability assessments and launch receipts in
the client. Runs `studio/checks/inference_receipt.check.mjs`, which drives
the real `studio/src/lib/cookbook/serve.ts` (`buildServePlan`) and
`studio/src/adapters/cookbook.ts` (`assessServe`, `serveModel`,
`getServeReceipt`, `verifyServeReceipt`, `summarizeReceipt`, `receiptTone`,
`baseUrlFromCmd`, `parseLaunchReceipt`) through esbuild rather than
re-implementing their logic in Python — same pattern
`tests/test_inf01_serve_js.py`/`tests/test_l69a_security_adapters_js.py` use.

Covers: `buildServePlan` reports the manifest's canonical option names
(never the form's own field spellings), sends the same full option set to
`llama-server` and `llama_cpp.server` alike (Python decides what a wrapper
omits, not this function), and returns `null` for a backend the manifest
does not cover; `serveModel` attaches the filed receipt and turns a 409
`serve.incompatible` into a typed `ServeIncompatibleError` carrying the
assessments; `getServeReceipt` turns a 404 into `null`; `verifyServeReceipt`
defaults `authorized_probe` to `false`; the three pure summary helpers.
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
def test_inference_receipt_js():
    result = subprocess.run(
        ["node", "studio/checks/inference_receipt.check.mjs"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL OK" in result.stdout
