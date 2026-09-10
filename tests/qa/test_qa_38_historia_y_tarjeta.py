"""QA-38 · Historia y tarjeta (docs/spec/v2/acceptance_scenarios.json).

Estimulo: restaurar desde historial una pregunta con objetos
label/description y multi.
Resultado exigido (literal): "Botones correctos, checklist y libre; mismo
resultado que en vivo."

Requisitos: EVAL-04, ACT-03.

Estado: verde. `src/question_store.py::Store.open/get` persiste la pregunta
completa - opciones {label, description}, `multi`, `allow_free_text` - tal
cual se genero en vivo (ver tests/test_ask_user_answer_route.py,
tests/test_question_store.py). Este test abre una pregunta multi-seleccion
con descripciones y texto libre, la relee como lo haria una vista de
historial, y comprueba que la forma es identica byte a byte a la que se
mostro en vivo.
"""
import pytest

from src.question_store import Store

pytestmark = pytest.mark.qa_state("green")


def test_a_question_reloaded_from_history_matches_the_live_shape(tmp_path):
    store = Store(tmp_path / "questions.sqlite3")
    live = store.open(
        "Que backends activamos?",
        session_id="s1",
        options=[
            {"label": "Docker", "description": "Aislado, requiere Docker Desktop"},
            {"label": "Local", "description": "Sin aislamiento adicional"},
            {"label": "Ambos", "description": "Docker cuando este disponible"},
        ],
        multi=True,
        allow_free_text=True,
    )

    # "Restore from history": a later read of the same question_id.
    restored = store.get(live["question_id"])

    assert restored["options"] == live["options"]
    assert restored["multi"] == live["multi"] is True
    assert restored["allow_free_text"] == live["allow_free_text"] is True
    assert restored["question"] == live["question"]
    # Same buttons (checklist, since multi=True) and the free-text line, byte
    # for byte the same as what was shown live.
    assert restored == live
