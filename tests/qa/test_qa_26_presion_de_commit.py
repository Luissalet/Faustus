"""QA-26 · Presion de commit (docs/spec/v2/acceptance_scenarios.json).

Estimulo: simular RAM/commit al limite con build y extraccion.
Resultado exigido (literal): "Pausa/reduce trabajos auxiliares antes de
cascada; conserva estado y UI."

Requisitos: EXEC-04, PERF-04.

Estado: verde. `src/bg_monitor.py` tiene ahora un vigilante de presion de
RAM/commit: `ram_pressure()` lee memoria disponible/total a traves de un
lector inyectable (mockeable, sin psutil real en el test);
`is_paused_for_resource_pressure()` aplica histeresis (pausa al superar
`RAM_PRESSURE_HIGH`, no se reanuda hasta caer por debajo de
`RAM_PRESSURE_LOW`) para no pausar/reanudar el mismo trabajo cada tick; y el
bucle de continuaciones en segundo plano (`_loop`) lo consulta ANTES de
invocar al agente para una continuacion auxiliar (research/embeddings/
audits que llegan por bg_jobs), saltandola en vez de arrancarla. El estado se
conserva gratis: el job saltado mantiene `followed_up=False` en el propio
store de bg_jobs -- el mismo mecanismo que ya usa la rama de apagado -- asi
que el siguiente tick lo recoge en cuanto la presion baja, en vez de
perderlo. `note_paused`/`note_resumed`/`paused_job_ids` recuerdan
explicitamente que trabajos estan aparcados y desde cuando.

Ver tests/test_hw_bg_monitor_ram_pressure.py para la cobertura completa
(lectura mockeada, histeresis, estado conservado, y el bucle real saltando/
retomando una continuacion).
"""
import asyncio

import pytest

from src import bg_monitor

pytestmark = pytest.mark.qa_state("green")


@pytest.fixture(autouse=True)
def _clean():
    bg_monitor.set_memory_reader(None)
    bg_monitor._paused_for_pressure = False
    bg_monitor._paused_jobs.clear()
    yield
    bg_monitor.set_memory_reader(None)
    bg_monitor._paused_for_pressure = False
    bg_monitor._paused_jobs.clear()


def test_a_ram_pressure_guard_pauses_auxiliary_jobs_before_a_cascade(monkeypatch):
    """Estimulo literal: RAM/commit al limite mientras hay una continuacion
    auxiliar pendiente (el equivalente aqui de "build y extraccion en
    curso"). Resultado: se pausa ANTES de invocar al agente (nunca se
    "ran"), followed_up se queda en False (estado conservado) y queda
    registrada como aparcada en vez de perderse en silencio."""
    bg_monitor.set_memory_reader(lambda: (2 * 2**30, 32 * 2**30))  # ~94% used

    ran = []
    monkeypatch.setattr(bg_monitor.bg_jobs, "pending_followups",
                        lambda: [{"id": "research-job-1", "status": "done"}])
    monkeypatch.setattr(bg_monitor.bg_jobs, "mark_followed_up",
                        lambda jid: ran.append(("marked", jid)))

    async def _fake_followup(rec):
        ran.append(("ran", rec["id"]))
        return True
    monkeypatch.setattr(bg_monitor, "_run_followup", _fake_followup)

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(bg_monitor._loop(stop))
        for _ in range(20):
            await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(run())

    assert ran == []  # never started, before any cascade
    assert "research-job-1" in bg_monitor.paused_job_ids()  # conserved, visible state
