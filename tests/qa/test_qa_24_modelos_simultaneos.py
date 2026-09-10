"""QA-24 · Modelos simultaneos (docs/spec/v2/acceptance_scenarios.json).

Estimulo: dos jobs solicitan cargar modelos grandes a la vez.
Resultado exigido (literal): "Admision con reservas; no prometer la misma
memoria a ambos."

Requisitos: HW-01.

Estado: xfail estricto. `src/vram_admission.py` no tiene ningun
`threading.Lock`/`asyncio.Lock` ni reserva atomica entre `assess()` /
`open_ticket()` (0 resultados en el modulo). Dos admisiones concurrentes
pueden leer el mismo presupuesto de VRAM libre y ambas abrir un ticket
convencidas de que cabe el modelo, exactamente la carencia que
docs/spec/v2/MAPA_REUTILIZACION.md (HW-01, parcial) documenta.
"""
import inspect

import pytest

from src import vram_admission

pytestmark = pytest.mark.qa_state("xfail")


@pytest.mark.xfail(
    strict=True,
    reason="vram_admission no reserva presupuesto de forma atomica: no hay "
           "Lock alrededor de assess()/open_ticket(), asi que dos admisiones "
           "concurrentes pueden leer el mismo presupuesto libre y prometer la "
           "misma memoria a ambos jobs.",
)
def test_ticket_admission_is_guarded_by_an_atomic_reservation():
    source = inspect.getsource(vram_admission)
    assert "Lock" in source
