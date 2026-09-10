"""QA-26 · Presion de commit (docs/spec/v2/acceptance_scenarios.json).

Estimulo: simular RAM/commit al limite con build y extraccion.
Resultado exigido (literal): "Pausa/reduce trabajos auxiliares antes de
cascada; conserva estado y UI."

Requisitos: EXEC-04, PERF-04.

Estado: xfail estricto. `routes/system_usage_routes.py` mide CPU/RAM/disco/
VRAM a nivel de host (psutil) y `src/disk_ballast.py` reacciona a presion de
DISCO, pero ningun modulo mide RSS/fds/threads del propio proceso Faustus ni
pausa trabajos auxiliares (research/builds/extraccion) cuando la RAM del
sistema se acerca al limite (EXEC-04/PERF-04 parcial segun
docs/spec/v2/MAPA_REUTILIZACION.md: "no hay psutil.Process(pid) propio... ni
limites explicitos de sesiones/jobs concurrentes").
"""
import pytest

pytestmark = pytest.mark.qa_state("xfail")


@pytest.mark.xfail(
    strict=True,
    reason="No existe un vigilante de presion de RAM/commit que pause o "
           "reduzca trabajos auxiliares (solo hay disk_ballast para presion "
           "de DISCO); EXEC-04/PERF-04 no cubren memoria del propio proceso.",
)
def test_a_ram_pressure_guard_pauses_auxiliary_jobs_before_a_cascade():
    from src import disk_ballast
    assert hasattr(disk_ballast, "memory_pressure_guard") or hasattr(disk_ballast, "ram_pressure_guard")
