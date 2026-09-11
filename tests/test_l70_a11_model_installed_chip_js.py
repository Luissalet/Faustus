"""Lote 70a, punto A.11 — `studio/src/lib/model-label.ts::isInstalled` and
the "not installed" chip it drives in `studio/src/screens/ModelPicker.tsx`
(PENDIENTES: "selector no marca no instalado").

`isInstalled(current, routes)` is exercised directly (no browser needed) by
`studio/checks/model-label.check.mjs`, run the same way
`tests/test_l69a_security_adapters_js.py` runs its own `.check.mjs`.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "model-label.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_model_label_check_js_passes():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all checks passed" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_model_picker_wires_the_not_installed_chip():
    """`ModelPicker.tsx` renders the chip from `isInstalled`, guarded on a
    loaded (non-empty) routes list so it never flashes true during the
    initial `listModels()` round trip."""
    src = (_REPO / "studio" / "src" / "screens" / "ModelPicker.tsx").read_text(encoding="utf-8")
    assert "isInstalled" in src
    assert "routes.length > 0" in src
    assert "not installed" in src.lower()
