"""QA-35 · Borrador persistente (docs/spec/v2/acceptance_scenarios.json).

Estimulo: escribir, adjuntar imagen, abrir editor, cambiar chat y recargar.
Resultado exigido (literal): "Texto/adjuntos/edicion vuelven; nada se envia
sin accion del usuario."

Requisitos: UX-01, MEDIA-03.

Estado: xfail estricto. `studio/src/screens/Studio.tsx` guarda el borrador
de TEXTO en `localStorage` (`readDraftFor`/`writeDraftFor`), pero los
adjuntos preparados (`attachments`, `useState`) viven solo en memoria de
React y se pierden al recargar (UX-01 parcial segun
docs/spec/v2/MAPA_REUTILIZACION.md: "Los adjuntos preparados... no se
restauran tras recargar"). El escenario exige texto Y adjuntos Y estado de
edicion; solo la primera mitad persiste.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.qa_state("xfail")

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.xfail(
    strict=True,
    reason="Los adjuntos del composer (attachments) no se persisten a "
           "localStorage/backend junto al texto del borrador; solo el texto "
           "sobrevive a una recarga.",
)
def test_composer_attachments_are_persisted_alongside_the_draft_text():
    studio_tsx = (REPO_ROOT / "studio" / "src" / "screens" / "Studio.tsx").read_text(encoding="utf-8")
    # A real fix would persist attachments the same way the draft text is:
    # something like writeDraftFor(..., {text, attachments}) or a dedicated
    # writeAttachmentsFor helper. Neither exists.
    assert re.search(r"writeDraftFor\([^)]*attachments", studio_tsx) or \
        re.search(r"writeAttachmentsFor", studio_tsx)
