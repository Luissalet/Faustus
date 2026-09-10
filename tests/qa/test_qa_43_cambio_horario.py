"""QA-43 · Cambio horario (docs/spec/v2/acceptance_scenarios.json).

Estimulo: recurrencia Europe/Madrid en transicion DST y equipo apagado.
Resultado exigido (literal): "Politica de ambiguedad/misfire visible; no
duplicacion silenciosa."

Requisitos: AUTO-01.

Estado: xfail estricto. `src/task_scheduler.py` usa `zoneinfo.ZoneInfo` para
calcular la proxima ejecucion, pero no declara ninguna politica explicita
para el instante ambiguo/inexistente del cambio de hora (el `fold`
attribute de `datetime` no aparece en el modulo) ni para un "misfire"
(equipo apagado durante la hora programada): al volver a arrancar, nada en
el codigo dice si la tarea perdida se dispara una vez, se salta, o podria
dispararse dos veces para la misma ocurrencia logica.
"""
import inspect

import pytest

from src import task_scheduler

pytestmark = pytest.mark.qa_state("xfail")


@pytest.mark.xfail(
    strict=True,
    reason="task_scheduler no declara una politica explicita de ambiguedad "
           "(datetime.fold) ni de misfire para una recurrencia perdida "
           "durante un cambio de horario con el equipo apagado.",
)
def test_scheduler_declares_an_explicit_dst_ambiguity_and_misfire_policy():
    source = inspect.getsource(task_scheduler)
    assert "fold" in source and "misfire" in source.lower()
