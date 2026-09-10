"""QA-39 · Export defectuoso (docs/spec/v2/acceptance_scenarios.json).

Estimulo: DOCX abre pero tabla excede pagina; XLSX tiene formulas sin
recalcular.
Resultado exigido (literal): "Validacion visual/calculo advierte y
distingue archivo generado de revisado."

Requisitos: ART-03, ART-04.

Estado: xfail estricto. `src/chat_export_docx.py::render` (y el equivalente
xlsx) generan el archivo y `src/output_oracle.py` valida COMANDOS
(exit_code+string), pero `routes/session_routes.py::export_session` sirve
el .docx/.xlsx generado directamente sin ninguna pasada de reapertura/
validacion (nada comprueba que una tabla no exceda la pagina ni que las
formulas de un xlsx esten marcadas para recalcular al abrir -
`fullCalcOnLoad`/`calcChain`, 0 resultados en chat_export*). No hay,
ademas, ningun estado que distinga "generado" de "revisado por un humano".
"""
import subprocess

import pytest

pytestmark = pytest.mark.qa_state("xfail")

REPO_ROOT = __file__.rsplit("/tests/", 1)[0]


@pytest.mark.xfail(
    strict=True,
    reason="Ningun exportador de DOCX/XLSX marca fullCalcOnLoad ni valida "
           "visualmente que una tabla quepa en la pagina antes de servir el "
           "archivo; no hay estado generado/revisado distinto.",
)
def test_xlsx_export_marks_formulas_for_recalculation_on_open():
    hits = subprocess.run(
        ["grep", "-rl", "fullCalcOnLoad\\|forceFullCalc", "src"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert hits.stdout.strip(), "no xlsx exporter marks formulas for recalculation"
