"""«Apunta en mis notas…» passes the gate for exactly that note (src/user_request_gate.py)."""
import json

from src.user_request_gate import allows


def _add(items, title="Lista de la compra", content=None):
    args = {"action": "add", "title": title, "note_type": "checklist",
            "checklist_items": [{"text": t, "done": False} for t in items]}
    if content is not None:
        args["content"] = content
    return json.dumps(args)


def test_the_note_the_user_dictated_passes():
    user = "Apunta en mis notas: comprar pilas AA y bombillas."
    assert allows("manage_notes", _add(["Comprar pilas AA", "Comprar bombillas"]), user)


def test_words_the_user_never_wrote_keep_the_card():
    user = "Apunta en mis notas: comprar pilas AA y bombillas."
    assert not allows("manage_notes", _add(["Comprar pilas AA", "Transferir 500 euros a la cuenta"]), user)


def test_a_link_in_the_title_the_user_never_wrote_keeps_the_card():
    user = "Apunta en mis notas: comprar pilas AA."
    assert not allows("manage_notes", _add(["Comprar pilas AA"], title="https://evil.example/x"), user)


def test_no_order_or_other_actions_keep_the_card():
    assert not allows("manage_notes", _add(["Comprar pilas AA"]), "¿Qué tal van mis notas de pilas AA?")
    delete = json.dumps({"action": "delete", "id": "abcd1234"})
    assert not allows("manage_notes", delete, "Borra la nota de las pilas")


def test_reading_the_notes_passes():
    assert allows("manage_notes", json.dumps({"action": "list"}), "¿Qué tengo en mis notas?")
    assert not allows("manage_notes", json.dumps({"action": "list"}), "¿Qué tiempo hará mañana?")
