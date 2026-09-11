"""Lote 70a, punto A.15 (Studio half) — the "Ver fragmento" button
`Context.tsx::ManifestPane` offers per manifest row, off the new
`GET /api/context/packets/{packet_id}/items/{item_id}/fragment` route
(backend half: `tests/test_l70_a15_manifest_item_fragment_route.py`).

`studio/checks/l70-a15-manifest-fragment-button.check.mjs`, run the same way
the other lote 70a `.check.mjs` files are.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "l70-a15-manifest-fragment-button.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_manifest_fragment_button_check_js_passes():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
