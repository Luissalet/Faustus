"""QA-34 · Olvido e incognito (docs/spec/v2/acceptance_scenarios.json).

Estimulo: corregir/borrar memoria y abrir turno incognito con herramientas.
Resultado exigido (literal): "No resurreccion desde cache; persistencia y
excepciones de archivos explicitos acordes a politica."

Requisitos: MEM-02, MEM-05.

Estado: xfail estricto. `src/memory_engine.py::delete_item` hace un DELETE
fisico + `_unindex`, sin ninguna tabla de tombstones (MEM-02 ausente segun
docs/spec/v2/MAPA_REUTILIZACION.md: "no hay tombstones ni politica de
resurreccion... el borrado es DELETE + reindex, sin registro de 'olvidado'
que sobreviva a un import"). Sin un tombstone, reconstruir el indice o
restaurar un backup anterior al borrado puede devolver el recuerdo que el
usuario corrigio/borro explicitamente - justo la resurreccion que el
enunciado prohibe. (MEM-05, incognito, si esta cubierto - ver
tests/test_chat_memory_recall_control.py - por eso este xfail se centra en
la mitad de tombstones.)
"""
import pytest

from src import memory_engine

pytestmark = pytest.mark.qa_state("xfail")


@pytest.mark.xfail(
    strict=True,
    reason="memory_engine.delete_item no deja tombstone: un import/reindex "
           "posterior al borrado puede resucitar un recuerdo que el usuario "
           "borro explicitamente.",
)
def test_deleting_an_item_leaves_a_tombstone_that_survives_reindexing():
    assert hasattr(memory_engine, "list_tombstones") or hasattr(memory_engine, "TOMBSTONE_TABLE")
