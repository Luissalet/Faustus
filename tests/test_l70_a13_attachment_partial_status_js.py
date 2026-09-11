"""Lote 70a, punto A.13 (Studio half) — `adapters/composer.ts::uploadFiles()`
decoding `status`/`partial`/`partial_reason` onto `Attachment`, and
`Composer.tsx` rendering the scanned/cover-only warning off it. The backend
half (routes/upload_routes.py forwarding the fields at all) is
`tests/test_l70_a13_upload_partial_status.py`.

`studio/checks/l70-a13-attachment-partial-status.check.mjs`, run the same
way the other lote 70a `.check.mjs` files are.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "l70-a13-attachment-partial-status.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_attachment_partial_status_check_js_passes():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
