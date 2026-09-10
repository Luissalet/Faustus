"""QA-08 · Reenvio de mensaje (docs/spec/v2/acceptance_scenarios.json).

Estimulo: doble clic en Enviar y caida de red antes del acuse.
Resultado exigido (literal): "Un Task y un mensaje confirmado; el outbox no
crea una segunda intencion."

Requisitos: UX-02, TASK-03.

Estado: verde. `src/chat_outbox.py::record_intent` ya cubre este escenario
literal (su propio docstring en tests/test_chat_idempotency.py cita
"QA-08"): la clave unica es `(owner, session_id, client_message_id)`, asi que
un doble clic (mismo `client_message_id`, dos llamadas) y una reconexion tras
caida de red (la misma llamada repetida porque el ack nunca llego) resuelven
a la MISMA fila en vez de crear una segunda intencion.
"""
import pytest

from src import chat_outbox

pytestmark = pytest.mark.qa_state("green")


@pytest.fixture()
def outbox_dir(tmp_path, monkeypatch):
    path = tmp_path / "chat_outbox.sqlite3"
    monkeypatch.setattr(chat_outbox, "default_path", lambda: path)
    return path


def test_double_click_and_dropped_ack_resolve_to_one_intent(outbox_dir):
    # Double click on Send: two calls, same client_message_id.
    first = chat_outbox.record_intent(owner="luis", session_id="s1", client_message_id="m-double-click")
    second = chat_outbox.record_intent(owner="luis", session_id="s1", client_message_id="m-double-click")
    assert first["created"] is True
    assert second["created"] is False, "the second click must not open a new intent"
    assert first["status"] == second["status"] == "accepted"

    # Network drops before the ack arrives; the client retries with the same
    # client_message_id - still the same row, not a second Task.
    retried = chat_outbox.record_intent(owner="luis", session_id="s1", client_message_id="m-double-click")
    assert retried["created"] is False

    # Exactly one confirmed message lands in the store for this id.
    chat_outbox.mark_finished(owner="luis", session_id="s1", client_message_id="m-double-click",
                               status="finished", result={"response": "ok"})
    row = chat_outbox.get(owner="luis", session_id="s1", client_message_id="m-double-click")
    assert row["status"] == "finished"
    assert row["result"] == {"response": "ok"}
