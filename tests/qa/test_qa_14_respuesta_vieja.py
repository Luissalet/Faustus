"""QA-14 · Respuesta vieja (docs/spec/v2/acceptance_scenarios.json).

Estimulo: resolver question_id anterior despues de cambiar el plan.
Resultado exigido (literal): "Rechazo por revision o decision obsoleta; no
responde a otra pregunta."

Requisitos: ACT-03.

Estado: verde. `src/question_store.py::Store.open/resolve` implementan esto
literalmente: `open(..., supersede_open=True)` cancela la pregunta anterior
todavia abierta cuando se hace una nueva ("el modelo preguntando algo nuevo
significa que la anterior ya no es la que responder" - el propio docstring
cita "QA-14's 'stale' case"), y `resolve()` comprueba `status`/`revision`
antes de aceptar cualquier respuesta.
"""
import pytest

from src.question_store import Store

pytestmark = pytest.mark.qa_state("green")


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "questions.sqlite3")


def test_resolving_a_superseded_question_id_is_rejected_not_misrouted(store):
    first = store.open("Usamos Redis o Postgres?", session_id="s1",
                        options=[{"label": "Redis"}, {"label": "Postgres"}])
    # The plan changes: the model asks a new question in the same session.
    second = store.open("Usamos Redis Streams o Kafka?", session_id="s1",
                         options=[{"label": "Redis Streams"}, {"label": "Kafka"}])
    assert first["question_id"] != second["question_id"]

    # A late answer to the FIRST (now stale) question_id must be refused -
    # never silently applied to whatever is "the most recent question".
    late_answer = store.resolve(first["question_id"], {"label": "Redis"})
    assert late_answer["ok"] is False
    assert late_answer["reason"] == "cancelled"  # superseded when the new question opened

    # And the second question is genuinely untouched by that rejected answer.
    still_open = store.get(second["question_id"])
    assert still_open["status"] == "open"
    assert still_open["answer"] is None


def test_resolving_with_a_stale_revision_is_rejected(store):
    q = store.open("Confirmas el despliegue?", session_id="s2",
                    options=[{"label": "Si"}, {"label": "No"}], revision=3)
    result = store.resolve(q["question_id"], {"label": "Si"}, revision=2)
    assert result["ok"] is False
    assert result["reason"] == "stale_revision"
    assert store.get(q["question_id"])["status"] == "open"
