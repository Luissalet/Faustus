"""QA-34 · Olvido e incognito (docs/spec/v2/acceptance_scenarios.json).

Estimulo: corregir/borrar memoria y abrir turno incognito con herramientas.
Resultado exigido (literal): "No resurreccion desde cache; persistencia y
excepciones de archivos explicitos acordes a politica."

Requisitos: MEM-02, MEM-05.

Estado: verde (lote 26). `src/memory_engine.py::forget` ahora deja un
tombstone (tabla `tombstones`, indexada por texto normalizado + alcance)
antes de borrar la fila y su embedding; `add_item` consulta ese tombstone y
rechaza una reinsercion automatica (`trust_class != "human_explicit"`) del
mismo texto en el mismo alcance -- que es exactamente la "resurreccion desde
cache" que un reindex/import/consolidacion podria producir. Una restitucion
EXPLICITA por parte del humano (`trust_class="human_explicit"`) sigue
funcionando, que es la excepcion de politica que el enunciado exige. Ver
tests/test_mem_tombstones.py para la bateria completa (tombstone
persistente, bloqueo de resurreccion automatica, excepcion humana explicita,
`correct()` con procedencia enlazada, limpieza del indice vectorial).

La mitad de incognito (MEM-05) ya estaba cubierta y sigue verde en
tests/test_chat_memory_recall_control.py: un turno incognito
(`suppress_personal_memory`) no llama a `memory_engine.pack_detail` ni deja
ids en `note_injected`, así que no hay nada que atribuir/escribir al cerrar
el turno.
"""
from datetime import datetime, timezone

import pytest

from src import memory_engine

pytestmark = pytest.mark.qa_state("green")

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_engine, "DATA_DIR", str(tmp_path))
    memory_engine.set_vector_store(None)
    memory_engine.clear_injected()
    yield tmp_path
    memory_engine.reset_vector_store()
    memory_engine.clear_injected()


def test_forgetting_a_memory_leaves_a_tombstone_that_blocks_reindex_resurrection(store):
    item = memory_engine.add_item(
        "The staging DB is read-only", owner="luis", project="acme",
        trust_class="human_explicit", now=NOW,
    )

    tombstone = memory_engine.forget(item["id"], reason="turned out to be wrong", now=NOW)
    assert tombstone is not None
    assert memory_engine.get_item(item["id"]) is None

    # "No resurreccion desde cache": an automated path (an extractor, a
    # consolidation/reindex pass) reproducing the same fact is refused...
    with pytest.raises(memory_engine.MemoryEngineError):
        memory_engine.add_item(
            "The staging DB is read-only", owner="luis", project="acme",
            trust_class="agent_assertion", now=NOW,
        )
    # ...and neither search nor the pack given to the model ever return it.
    assert not memory_engine.search("staging DB read-only", "luis", "acme", now=NOW)
    detail = memory_engine.pack_detail("luis", "acme", "staging DB read-only", 2000, now=NOW)
    assert item["id"] not in detail["ids"]

    # "...persistencia y excepciones de archivos explicitos acordes a
    # politica": an EXPLICIT human restatement is still allowed.
    revived = memory_engine.add_item(
        "The staging DB is read-only", owner="luis", project="acme",
        trust_class="human_explicit", now=NOW,
    )
    assert revived["text"] == "The staging DB is read-only"
