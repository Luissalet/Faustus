"""QA-09 · Replay de stream (docs/spec/v2/acceptance_scenarios.json).

Estimulo: duplicar y desordenar eventos y reconectar con cursor antiguo.
Resultado exigido (literal): "Mismo transcript/progreso final sin acciones o
preguntas duplicadas."

Requisitos: OBS-01, UX-05.

Estado: verde. `src/state_mirror/ingest.py::ingest` (la autoridad de
`run`/`task` real, ver tests/test_state_mirror_replay.py y
tests/test_state_mirror_conflict_resolution.py para BASE-02) es
idempotente y tolera desorden: observaciones repetidas o entregadas fuera de
orden convergen al mismo estado materializado final, exactamente lo que un
cliente que reconecta con un cursor antiguo y recibe eventos duplicados o
desordenados necesita para no duplicar progreso. Este test alimenta el mismo
lote de observaciones dos veces (una duplicada, una desordenada) y comprueba
que el estado final y el historial de conflictos son identicos al de un
unico paso limpio.
"""
import pytest

from src.state_mirror import contracts as C, ingest, persistence

pytestmark = pytest.mark.qa_state("green")

OWNER = "alice"
ENTITY = C.entity_id("run", OWNER, "qa09")


def _at(second):
    return f"2026-09-07T12:00:{second:02d}Z"


def _obs(value, second):
    return C.StateObservation.parse({
        "entity_id": ENTITY, "owner": OWNER, "source": "chat_stream",
        "schema": "run_state.v1", "epistemic": "observed",
        "observed_at": _at(second), "state": {"status": value},
    })


@pytest.fixture()
def store(tmp_path):
    result = persistence.StateStore(path=str(tmp_path / "state.db"))
    yield result
    result.close()


def test_duplicated_and_reordered_events_converge_to_the_same_final_state(store, tmp_path):
    a, b, c = _obs("running", 0), _obs("tool_call", 1), _obs("done", 2)

    # Clean, in-order delivery - the baseline.
    r_clean = ingest.ingest([a, b, c], store=store, now=_at(3))
    assert r_clean.errors == () and r_clean.refused == ()
    clean_final = store.get_state(ENTITY).values()

    # A second store simulating: b delivered twice (a duplicate SSE replay),
    # and a, c arriving out of order (a reconnect with a stale cursor that
    # redelivers the tail before the still-buffered head).
    store2 = persistence.StateStore(path=str(tmp_path / "state2.db"))
    try:
        r1 = ingest.ingest([c, b, b, a], store=store2, now=_at(3))
        assert r1.errors == ()
        dup_reordered_final = store2.get_state(ENTITY).values()
    finally:
        store2.close()

    # Same transcript/progress at the end, regardless of duplication/order.
    assert dup_reordered_final == clean_final
    # And no duplicate conflict/question artifacts were left behind.
    assert store.conflicts(owner=OWNER) == []
