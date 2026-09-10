"""QA-32 · Preview malicioso (docs/spec/v2/acceptance_scenarios.json).

Estimulo: HTML/SVG intenta usar IPC o ejecutar scripts privilegiados.
Resultado exigido (literal): "Sandbox de preview y CSP impiden acceso al
equipo y al origen de la app."

Requisitos: SEC-07.

Estado: verde. `desktop/main.cjs` (SEC-07, existente) crea toda ventana con
`contextIsolation:true, sandbox:true, nodeIntegration:false, webSecurity:true`
- sin puente a Node/IPC desde contenido no confiable - y cada vista de
preview HTML en Studio (`Editor.tsx`/`Compare.tsx`/`Reader.tsx`) renderiza el
contenido en un `<iframe sandbox="...">` en vez de inyectarlo en el DOM de
la propia app, aislandolo del origen de Faustus. Este test comprueba
directamente el codigo fuente real desplegado (no una copia), para que
cualquiera de esas lineas que se borre haga fallar el test.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.qa_state("green")

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_every_browser_window_disables_node_integration_and_enables_the_sandbox():
    main_cjs = (REPO_ROOT / "desktop" / "main.cjs").read_text(encoding="utf-8")
    window_blocks = re.findall(r"webPreferences:\{[^}]*\}", main_cjs)
    assert window_blocks, "no BrowserWindow webPreferences found"
    for block in window_blocks:
        assert "nodeIntegration:false" in block
        assert "contextIsolation:true" in block
        assert "sandbox:true" in block


@pytest.mark.parametrize("relpath", [
    "studio/src/screens/documents/Editor.tsx",
    "studio/src/screens/compare/Compare.tsx",
    "studio/src/screens/email/Reader.tsx",
    # Lote 50: BENCH-06's HTML/SVG canvas preview is a fourth HTML preview
    # surface (tests/test_p1_bench06_preview.py already checks it in
    # isolation) — QA-32's own parametrize list must cover it too so deleting
    # its sandbox attribute fails the SAME test as the other three surfaces.
    "studio/src/components/Preview.tsx",
])
def test_html_preview_surfaces_render_inside_a_sandboxed_iframe(relpath):
    source = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    if relpath.endswith("components/Preview.tsx"):
        # BENCH-06's sandbox value is computed (studio/src/components/
        # previewSandbox.ts::sandboxAttr), not a literal string in the JSX,
        # so this surface is checked against that helper's own source
        # instead of the `sandbox="..."` literal the other three use.
        assert re.search(r'<iframe[^>]*\bsandbox=\{sandboxAttr\(', source), (
            f"no sandboxed <iframe> found in {relpath}")
        helper = (REPO_ROOT / "studio/src/components/previewSandbox.ts").read_text(encoding="utf-8")
        fn = re.search(r"export function sandboxAttr\([^)]*\)[^{]*\{.*?\n\}", helper, re.S)
        assert fn, "sandboxAttr() not found in previewSandbox.ts"
        assert "allow-same-origin" not in fn.group(0), (
            "sandboxAttr() must never grant allow-same-origin")
        return
    matches = re.findall(r'<iframe[^>]*\bsandbox="([^"]*)"', source)
    assert matches, f"no sandboxed <iframe> found in {relpath}"
    for attrs in matches:
        # Never blanket-allow same-origin AND scripts together - that would
        # let a malicious preview reach the app's own origin/localStorage.
        assert not ("allow-same-origin" in attrs and "allow-scripts" in attrs)
