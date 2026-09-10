"""QA-10 · Reinicio en research (docs/spec/v2/acceptance_scenarios.json).

Estimulo: matar backend tras guardar tres rondas y una seccion.
Resultado exigido (literal): "Marca interrupted; Reanudar reutiliza
evidencia confirmada; Reintentar empieza otra ejecucion explicita."

Requisitos: TASK-02, RES-05.

Estado: xfail estricto. `src/research_handler.py::ResearchHandler` marca
`interrupted` tras un reinicio (`recover_interrupted`,
`list_interrupted` - TASK-02, probado en tests/test_research_restart_survival.py)
pero NO existe un metodo "resume"/"reanudar" que reconstruya por
rondas/fuentes/secciones ya confirmadas: solo hay recuperacion de la
interrupcion y reintento desde cero (RES-05 ausente segun
docs/spec/v2/MAPA_REUTILIZACION.md: "un reinicio de backend pierde el
progreso y 'Retry' vuelve a gastar toda la inferencia"). No hay, por tanto,
distincion entre Reanudar y Reintentar en el codigo.
"""
import pytest

from src.research_handler import ResearchHandler

pytestmark = pytest.mark.qa_state("xfail")


@pytest.mark.xfail(
    strict=True,
    reason="ResearchHandler no tiene un metodo resume/reanudar que reutilice "
           "rondas/secciones confirmadas tras un reinicio; solo "
           "recover_interrupted (marca interrupted) y una repeticion completa.",
)
def test_a_resume_method_reuses_confirmed_rounds_and_sections():
    assert hasattr(ResearchHandler, "resume_interrupted")
