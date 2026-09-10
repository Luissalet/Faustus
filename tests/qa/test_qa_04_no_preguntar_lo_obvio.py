"""QA-04 · No preguntar lo obvio (docs/spec/v2/acceptance_scenarios.json).

Estimulo: pedir sustituir texto exacto de un archivo existente.
Resultado exigido (literal): "Ejecuta la edicion acotada sin dialogo de
arquitectura."

Requisitos: CALL-07, LANG-01.

Estado: xfail estricto. `EditFileTool` y `AskUserTool` (ambos en
src/agent_tools/) son herramientas independientes: nada en el codigo obliga
a que el modelo elija la primera en vez de la segunda ante una peticion
literal - esa decision vive en el prompt del bucle del agente
(src/agent_loop.py), no en un contrato verificable sin invocar un modelo real
o simulado. El repo tiene un arnes e2e con LLM simulado
(tests/e2e/conftest.py::fake_llm, usado por EVAL-04/EVAL-05) capaz de probar
esto end-to-end, pero no existe todavia un guion fake_llm para el caso
literal "sustituir texto exacto no debe disparar ask_user". Documentar el
guion que falta en vez de simularlo aqui con un mock del propio agente evita
inflar el veredicto a verde por una prueba que no ejercita el codigo real de
decision.
"""
import importlib

import pytest

pytestmark = pytest.mark.qa_state("xfail")


@pytest.mark.xfail(
    strict=True,
    reason="No hay un guion e2e (fake_llm) que pruebe que una sustitucion de "
           "texto exacto NUNCA dispara ask_user; la decision de que herramienta "
           "usar vive solo en el prompt de src/agent_loop.py, sin contrato "
           "verificable en codigo.",
)
def test_e2e_fixture_proves_literal_edits_never_trigger_ask_user():
    fake_llm = importlib.import_module("tests.e2e.fake_llm")
    assert hasattr(fake_llm, "literal_edit_never_asks")
