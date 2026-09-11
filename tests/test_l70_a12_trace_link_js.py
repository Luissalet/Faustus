"""Lote 70a, punto A.12 — the tool card's `call_id` and its "Ver traza" link
to `/activity?trace=<call_id>&session=<sessionId>` (`Activity.tsx::TracePanel`
already reads that query string; it just never had a call_id offered to it).

`studio/checks/l70-a12-trace-link.check.mjs`, run the same way
`tests/test_l29_studio_error_trace_decode_js.py` runs `l29-error-trace-decode`.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "l70-a12-trace-link.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_trace_link_check_js_passes():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
