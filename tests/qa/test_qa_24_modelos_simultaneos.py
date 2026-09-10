"""QA-24 · Modelos simultaneos (docs/spec/v2/acceptance_scenarios.json).

Estimulo: dos jobs solicitan cargar modelos grandes a la vez.
Resultado exigido (literal): "Admision con reservas; no prometer la misma
memoria a ambos."

Requisitos: HW-01.

Estado: verde. `src/vram_admission.py` ahora tiene `try_reserve()`/
`release_reservation()`/`release_reservations_for_model()`: un test-and-set
atomico bajo `threading.Lock` (`_RES_LOCK`), por `(root, device)` -- pool
completo con `device=None`, una GPU con un indice. `assess()` descuenta las
reservas activas del presupuesto que reporta; `admit()` reserva el hueco de
un modelo justo antes de devolver "proceed" y, si la reserva falla porque
otro `admit()` concurrente ya se la quedo, trata el caso exactamente como
"no cabe" (abre ticket en modo ask / intenta liberar en modo auto) en vez de
dejarlos competir por los mismos bytes. La reserva se libera en cuanto
`assess()` ve el modelo residente (la comprobacion barata de /api/ps que ya
hace en cada llamada) o al caducar `RESERVATION_TTL_SECONDS`.

Ver tests/test_hw_vram_reservation.py para la cobertura completa (el
primitivo reserve/release/expiry/por-dispositivo, `assess()` descontando
reservas, y este mismo escenario de dos `admit()` concurrentes).
"""
import asyncio

import pytest

from src import vram_admission as va

pytestmark = pytest.mark.qa_state("green")

GIB = 2**30
ROOT = "http://127.0.0.1:11434"
EP = ROOT + "/v1/chat/completions"

BIG_A = {"name": "big-a:27b", "size": 18 * GIB, "digest": "d-a"}
BIG_B = {"name": "big-b:27b", "size": 18 * GIB, "digest": "d-b"}


def _card(total_gb: float, used_gb: float):
    return {"supported": True, "name": "GeForce", "total": int(total_gb * GIB),
            "used": int(used_gb * GIB), "free": int((total_gb - used_gb) * GIB),
            "gpus": [{"index": 0, "name": "GeForce", "uuid": "u0", "total": int(total_gb * GIB),
                      "used": int(used_gb * GIB), "free": int((total_gb - used_gb) * GIB)}]}


@pytest.fixture(autouse=True)
def clean_tables():
    va._RESERVATIONS.clear()
    va._PENDING.clear()
    yield
    va._RESERVATIONS.clear()
    va._PENDING.clear()


def test_admission_reserves_memory_atomically_so_two_jobs_never_get_the_same(monkeypatch):
    """Estimulo literal: dos jobs piden cargar, a la vez, dos modelos grandes
    que caben cada uno por separado en la tarjeta vacia pero no los dos
    juntos. Resultado: uno se admite, el otro entra en la negociacion normal
    de "no cabe" (aqui, modo ask: se le abre un ticket y se le deja
    esperando) -- nunca los dos con "proceed" a la vez."""
    def _get(root, path, timeout):
        return {"models": [BIG_A, BIG_B]} if path == "/api/tags" else {"models": []}
    monkeypatch.setattr(va, "_get", _get)
    monkeypatch.setattr("src.gpu_shared_memory.vram_snapshot", lambda: _card(24, 0.2))
    monkeypatch.setattr("src.gpu_placement.placement", lambda root, loaded, gpus: {})

    async def run():
        return await asyncio.gather(
            va.admit(EP, BIG_A["name"], owner="a", mode="ask", timeout=0.2),
            va.admit(EP, BIG_B["name"], owner="b", mode="ask", timeout=0.2),
            return_exceptions=True,
        )

    results = asyncio.run(run())
    proceeded = [r for r in results if r == "proceed"]
    cancelled = [r for r in results if isinstance(r, va.AdmissionCancelled)]
    assert len(proceeded) == 1, results
    assert len(cancelled) == 1, results
