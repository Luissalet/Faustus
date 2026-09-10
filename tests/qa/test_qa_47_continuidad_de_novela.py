"""QA-47 · Continuidad de novela (docs/spec/v2/acceptance_scenarios.json).

Estimulo: crear final alternativo, descartarlo y escribir capitulo
siguiente.
Resultado exigido (literal): "Canon original conservado; alternativa no se
recupera como hecho confirmado."

Requisitos: WRITE-02, WRITE-04.

Estado: verde. `src/branching_futures/narrative_canon.py` (nuevo) anade el
vocabulario canon/alternativa sobre `BranchingService`, que ya modelaba
exactamente esta forma (explorar varias estrategias para el mismo punto de
la trama, someter un resultado observado por cada una, seleccionar una) sin
tener el concepto de "canon" que el escenario exige. `alternative_status`
deriva `draft|discarded|promoted` del estado que `select()`/`cancel()` ya
persisten (ninguna fila nueva se escribe solo para este marcado);
`canon_state(project)` es la unica funcion construida SOLO a partir de
branches `promoted` — no hay ningun camino de codigo que copie el resultado
de una alternativa descartada dentro de ella, asi que un resumen o recall
posterior que solo lea `canon_state()` no puede resucitarla como hecho
confirmado. Este test reproduce el escenario QA-47 literal con un proyecto
sintetico: crea un final alternativo (2 estrategias), somete resultado para
ambas, selecciona la original (descarta la alternativa), y comprueba que el
texto de la alternativa no aparece en ningun lugar de `canon_state()` aunque
si aparece, marcado como `discarded`, en `discarded_alternatives()`. La
prueba completa (mas casos: abandonar sin seleccionar, aislamiento por
proyecto/owner, `alternative_status` en cada transicion) vive en
tests/test_write_narrative_canon.py.

Pendiente fuera de este lote (ver el informe del lote 41): nada fuera de
`src/branching_futures` llama hoy a `canon_state()` — `src/memory_engine.py`
y `src/context_engine/` (los modulos que el xfail original citaba) seguirian
pudiendo recuperar una alternativa descartada de la transcripcion cruda si
un proyecto usa branching futures para su narrativa en vez de (o ademas de)
escribir directamente; el punto de enganche exacto queda documentado en el
informe, ninguno de los dos ficheros es PROPIO de este lote.
"""
import pytest

from src.branching_futures.narrative_canon import canon_state, discarded_alternatives
from src.branching_futures.service import BranchingService
from src.durable_feature_store import DurableFeatureStore

pytestmark = pytest.mark.qa_state("green")


def test_a_discarded_alternate_ending_is_marked_and_never_resurfaces_as_canon(tmp_path):
    novel = BranchingService(DurableFeatureStore(str(tmp_path / "qa47.db")))

    future = novel.create(owner="author", project_id="the-novel", request={
        "title": "Chapter 12 — the ending",
        "intent": "Decide how the confrontation resolves",
        "mode": "simulate",
        "strategies": [
            {"id": "original", "title": "Elena forgives her brother"},
            {"id": "alt_death", "title": "Elena's brother dies in the fire"},
        ],
    })
    original, alt = future["branches"]

    for branch_id, summary in (
        (original["id"], "Elena forgives her brother by the riverbank."),
        (alt["id"], "Elena's brother perishes in the warehouse fire."),
    ):
        novel.start_branch(owner="author", future_id=future["id"], branch_id=branch_id)
        novel.submit_result(owner="author", future_id=future["id"], branch_id=branch_id,
                            result={"status": "completed", "summary": summary,
                                    "scores": {"quality": 1}})

    novel.evaluate(owner="author", future_id=future["id"])
    # Crear un final alternativo, descartarlo: seleccionar el original
    # descarta explicitamente el otro.
    novel.select(owner="author", future_id=future["id"], branch_id=original["id"],
                rationale="Keeps the reconciliation arc the rest of the book sets up.")

    # Escribir el capitulo siguiente: una nueva exploracion para el mismo
    # proyecto (no depende en nada del branch descartado).
    novel.create(owner="author", project_id="the-novel", request={
        "title": "Chapter 13", "intent": "Continue from the reconciliation",
        "mode": "simulate",
        "strategies": [{"id": "a", "title": "Scene A"}, {"id": "b", "title": "Scene B"}],
    })

    # Canon original conservado.
    canon = canon_state(owner="author", project_id="the-novel", svc=novel)
    assert len(canon["chapters"]) == 1
    assert canon["chapters"][0]["branch_id"] == original["id"]

    # La alternativa descartada no vuelve como hecho confirmado, en ningun
    # lugar de lo que canon_state() expone.
    canon_text = repr(canon)
    assert "perishes" not in canon_text and "fire" not in canon_text
    assert alt["id"] not in canon_text

    # ... pero sigue siendo auditable, explicitamente marcada como descartada.
    discarded = discarded_alternatives(owner="author", project_id="the-novel", svc=novel)
    assert [row["branch_id"] for row in discarded] == [alt["id"]]
    assert discarded[0]["status"] == "discarded"
