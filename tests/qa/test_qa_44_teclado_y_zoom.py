"""QA-44 · Teclado y zoom (docs/spec/v2/acceptance_scenarios.json).

Estimulo: completar chat, permisos y diff solo teclado a 200% zoom.
Resultado exigido (literal): "Foco util, controles accesibles y ningun boton
critico fuera de alcance."

Requisitos: A11Y-01, A11Y-03.

Estado: xfail estricto (Lote 39). `scripts/ui_a11y.py` (nuevo, Playwright)
SI automatiza el recorrido completo con Chromium headless -- chat, tarjeta
de aprobacion, diff y un dialogo real, solo con teclado, a 100% y 200% de
zoom -- contradiciendo la premisa anterior de que esto "no [es] algo que un
test headless de Playwright... reproduzca por si solo". Se ejecuto de
verdad contra un servidor real (ver logs/ui_a11y/result.json, generado en
esta misma ronda): 34/38 comprobaciones en verde. Arreglado en el propio
Transcript.tsx/studio.css donde se encontraron huecos reales: `DiffLines`
no tenia `tabIndex`/`role` (region desplazable inalcanzable por teclado);
la tarjeta de aprobacion/pregunta (`AskCard`/`QuestionCard`) no se
desplazaba a la vista al aparecer, dejando sus botones fuera del viewport a
200% zoom; el `<details>` del tool rail no se auto-encuadraba al abrirse.

Lo que SIGUE fallando de verdad, en ficheros ajenos a este lote (no
tocados, ver el informe del lote para el cambio exacto):
  - `studio/src/screens/studio/WorkspaceDialog.tsx` (via el `Dialog`
    compartido, `studio/src/components/Dialog.tsx`): su contenido puede
    desbordar el borde DERECHO del viewport a 200% de zoom (encontrado en
    vivo: rect.right 1537 > viewport width 1280) -- necesita un
    `max-inline-size` relativo al viewport, no solo al contenido.
  - la region del diff (`.fs-diff`, Transcript.tsx/studio.css, YA
    mitigado con `scrollIntoView` en foco/apertura) puede quedar unos ~20px
    por debajo del pliegue en un viewport MUY pequeno (860px logicos / 2 =
    430px reales) combinado con contenido largo encima -- residual, no
    eliminado del todo; ver el informe del lote.

Este test se queda en xfail (no verde) porque el criterio de aceptacion es
literal y absoluto ("ningun boton critico fuera de alcance") y esas dos
cosas lo incumplen de verdad, una de ellas en un fichero que este lote no
puede tocar.

Requiere `ODYSSEUS_E2E=1` (misma convencion que tests/e2e/) + playwright +
chromium instalados; sin eso, se salta con un motivo claro en vez de dar un
falso verde. Con `ODYSSEUS_E2E=1`, EJECUTA `scripts/ui_a11y.py` de verdad
(subprocess) contra un servidor real, no una version recortada.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.qa_state("xfail")

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "ui_a11y.py"
E2E = os.environ.get("ODYSSEUS_E2E", "").strip().lower() in {"1", "true", "yes", "on"}

try:  # pragma: no cover - import guard, mirrors tests/e2e/conftest.py
    import playwright  # noqa: F401
    _HAS_PW = True
except Exception:  # noqa: BLE001
    _HAS_PW = False


def test_ui_a11y_script_exists_and_documents_the_manual_walkthrough():
    """Always runs (no browser needed): the script is real Python, and even
    when it cannot run here it still names the steps a human can follow."""
    assert SCRIPT.exists()
    source = SCRIPT.read_text(encoding="utf-8")
    assert "playwright" in source.lower()
    assert "100" in source and "200" in source  # both zoom levels
    compiled = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr


@pytest.mark.skipif(not (E2E and _HAS_PW and shutil.which("node")), reason="set ODYSSEUS_E2E=1 with playwright+chromium installed to run the real keyboard/zoom walkthrough")
@pytest.mark.xfail(
    strict=True,
    reason="Real, reproducible gaps remain at 200% zoom: WorkspaceDialog.tsx "
           "(fichero ajeno) can overflow the viewport's right edge, and the "
           "diff region can sit a few px below the fold in a very small "
           "viewport even after this lote's scrollIntoView fixes — see the "
           "module docstring and this lote's report.",
)
def test_keyboard_only_walkthrough_at_100_and_200_percent_zoom():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=REPO, capture_output=True, text=True, timeout=280,
    )
    print(result.stdout)
    print(result.stderr, file=__import__("sys").stderr)
    report_path = REPO / "logs" / "ui_a11y" / "result.json"
    assert report_path.exists(), "scripts/ui_a11y.py did not write logs/ui_a11y/result.json"
    data = json.loads(report_path.read_text(encoding="utf-8"))
    failed = [c for c in data["checks"] if not c["ok"]]
    assert not failed, f"{len(failed)}/{data['total']} keyboard/zoom checks failed: {failed}"
