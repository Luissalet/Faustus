"""QA-28 · Cambio de modelo (docs/spec/v2/acceptance_scenarios.json).

Estimulo: cambiar a modelo sin vision/tool nativo durante una tarea.
Resultado exigido (literal): "Recalcula capacidades, reconstruye estado y no
simula capacidades perdidas."

Requisitos: MOD-06, MEDIA-01.

Estado: xfail estricto. `src/agent_loop.py::_agent_route_tool_mode` decide
nativo/restringido por una lista de palabras clave del NOMBRE del modelo, no
por una calibracion medida (MOD-02, que no existe segun
docs/spec/v2/MAPA_REUTILIZACION.md). No hay ninguna funcion que, al detectar
un cambio de modelo a mitad de tarea, recalcule explicitamente las
capacidades perdidas (vision/tool nativo) y reconstruya el estado del turno
en consecuencia - solo hay una heuristica de enrutado de herramientas al
inicio de cada llamada.
"""
import inspect

import pytest

from src import agent_loop

pytestmark = pytest.mark.qa_state("xfail")


@pytest.mark.xfail(
    strict=True,
    reason="No hay una funcion dedicada a recalcular capacidades y "
           "reconstruir el estado del turno cuando el modelo cambia a mitad "
           "de tarea; _agent_route_tool_mode es heuristica por nombre, no "
           "calibracion, y no hay reconstruccion de estado explicita.",
)
def test_switching_models_mid_task_recomputes_capabilities_and_rebuilds_state():
    assert hasattr(agent_loop, "recompute_capabilities_on_model_switch")
