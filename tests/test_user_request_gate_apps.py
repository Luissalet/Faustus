"""«Lo que pides tú, pasa», for the user's own apps.

Every request through a Hoard plugin stopped at "Allow this task to
continue?": the tool's description arms the external-context gate on the
first turn, and no matcher knew MCP tools. A call to a connected app's tool
passes when the user's latest words name the act (a `Sinónimos:` phrase or
the tool's name) and, for a tool that writes, one of the call's own values.
"""
import pytest

from src import user_request_gate as gate

CARDS_DUE = {
    "name": "cards_due",
    "description": "The cards due for review right now.\nSinónimos: repasar, tarjetas pendientes, quiz, examíname, pregúntame, hoy toca repasar.",
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}
ADD_ENTRY = {
    "name": "add_entry",
    "description": "Record a movement (write).\nSinónimos: apunta, anota un gasto, he pagado, me han pagado, registra.",
    "annotations": {"readOnlyHint": False, "destructiveHint": False},
}
SAVE_LINK = {
    "name": "save_link",
    "description": "Save a URL to the read-later library.\nSinónimos: guarda esto, guardar enlace, para luego, guarda este artículo.",
    "annotations": {"readOnlyHint": False, "destructiveHint": False},
}
SCREEN_SEARCH = {
    "name": "screen_search",
    "description": "Search the screen memory.",  # no synonyms line: the name's words must do
    "annotations": None,
}


CARD_REVIEW = {
    "name": "card_review",
    "description": "Grade one card the user just answered in chat (write).\nSinónimos: calificar, puntuar tarjeta.",
    "annotations": {"readOnlyHint": False, "destructiveHint": False},
}
CARD_DELETE = {
    "name": "card_delete",
    "description": "Permanently delete a card (write, destructive).\nSinónimos: borrar tarjeta.",
    "annotations": {"readOnlyHint": False, "destructiveHint": True},
}


class _Mcp:
    _tools = {"4f9230b5": [CARDS_DUE, CARD_REVIEW, CARD_DELETE], "10d9867d": [ADD_ENTRY], "41bff0fe": [SAVE_LINK], "df4c1bc4": [SCREEN_SEARCH],
              "notmine1": [CARDS_DUE]}


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    connectors = {"4f9230b5", "10d9867d", "41bff0fe", "df4c1bc4"}
    monkeypatch.setattr("src.connector_sidecar.get_connector_for_server",
                        lambda server_id, redact=True: {"id": "c-" + server_id} if server_id in connectors else None)
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: _Mcp())


def test_a_read_tool_passes_when_the_user_named_the_act():
    assert gate.allows("mcp__4f9230b5__cards_due", '{"deck": "Faustus", "limit": 10}',
                       "Examíname con las tarjetas pendientes del mazo Faustus.")
    assert gate.allows("mcp__4f9230b5__cards_due", "{}", "¿Qué tarjetas pendientes tengo hoy?")


def test_a_read_tool_keeps_the_gate_without_the_act():
    assert not gate.allows("mcp__4f9230b5__cards_due", "{}", "Hola, ¿qué tal?")
    assert not gate.allows("mcp__4f9230b5__cards_due", "{}", "")


def test_a_write_tool_needs_the_act_and_one_of_its_own_values():
    ok = "Apunta 12,50 € de taxi en efectivo."
    assert gate.allows("mcp__10d9867d__add_entry", '{"amount": 12.5, "category": "taxi", "account": "Efectivo"}', ok)
    # the amount alone, said with a comma and two decimals
    assert gate.allows("mcp__10d9867d__add_entry", '{"amount": 12.5, "category": "transporte"}', "Apunta 12,50 de esta mañana.")
    # the act without any value the user said: the model chose them all
    assert not gate.allows("mcp__10d9867d__add_entry", '{"amount": 12.5, "category": "taxi"}', "Apunta el gasto de antes.")
    # a value without the act
    assert not gate.allows("mcp__10d9867d__add_entry", '{"amount": 12.5, "category": "taxi"}', "El taxi costó 12,50.")


def test_period_words_and_short_values_do_not_count_as_targets():
    assert not gate.allows("mcp__10d9867d__add_entry", '{"amount": 5, "category": "hoy"}', "Apunta lo de hoy.")


def test_a_url_the_user_pasted_is_a_target():
    url = "https://example.org/articulo-largo"
    assert gate.allows("mcp__41bff0fe__save_link", '{"url": "%s", "tags": ["lectura"]}' % url,
                       f"Guarda este enlace con la etiqueta lectura y dime de qué va: {url}")


def test_a_synonym_matches_by_stem_and_order_not_letter_by_letter():
    # "guardar enlace" said as "guarda este enlace"; "repasar" as "repasemos"
    assert gate.allows("mcp__4f9230b5__cards_due", "{}", "Repasemos un rato.")
    # the words out of order or far apart do not make the phrase
    assert not gate.allows("mcp__4f9230b5__cards_due", "{}", "Las pendientes de la casa y luego las tarjetas.")


def test_the_tool_name_words_count_when_there_is_no_synonyms_line():
    assert gate.allows("mcp__df4c1bc4__screen_search", '{"q": "vulcan"}', "Haz un screen search de vulcan.")
    assert not gate.allows("mcp__df4c1bc4__screen_search", '{"q": "vulcan"}', "¿Qué estaba haciendo hace diez minutos?")


def test_an_mcp_server_that_is_not_one_of_the_users_connectors_keeps_the_gate():
    assert not gate.allows("mcp__notmine1__cards_due", "{}", "Examíname con las tarjetas pendientes.")


def test_unknown_tools_and_builtins_are_unchanged():
    assert not gate.allows("mcp__4f9230b5__nope", "{}", "Examíname con las tarjetas pendientes.")
    assert not gate.allows("web_fetch", '{"url": "https://example.org"}', "Examíname con las tarjetas pendientes.")


QUESTION = "Empezamos con la primera:\n\n**¿Qué puede hacer Faustus con una app conectada?**\n\nCuéntame tu respuesta."


def test_answering_the_question_just_asked_lets_the_grading_call_through():
    call = '{"front": "¿Qué puede hacer Faustus con una app conectada?", "grade": 2}'
    answer = "Leer la lista de plugins y arrancar una app parada."
    assert gate.allows("mcp__4f9230b5__card_review", call, answer, "", QUESTION)
    # without the question in the previous message the answer names no act
    assert not gate.allows("mcp__4f9230b5__card_review", call, answer, "", "")
    # a call whose values are not in what was asked
    other = '{"front": "¿Dónde vive el manifiesto de un plugin?", "grade": 2}'
    assert not gate.allows("mcp__4f9230b5__card_review", other, answer, "", QUESTION)
    # never a destructive tool
    assert not gate.allows("mcp__4f9230b5__card_delete", call, answer, "", QUESTION)


def test_asked_before_is_the_assistant_message_the_user_replied_to():
    messages = [
        {"role": "user", "content": "Examíname."},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "{...}"},
        {"role": "assistant", "content": QUESTION},
        {"role": "user", "content": "No me acuerdo."},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "2"}]},
    ]
    assert gate.asked_before_text(messages) == QUESTION
    assert gate.asked_before_text([{"role": "user", "content": "Hola"}]) == ""


def test_reading_the_skill_the_request_points_at_passes(monkeypatch):
    class _Skills:
        def __init__(self, data_dir):
            pass

        def load(self, owner=None):
            return [{"name": "hoard-study-cards",
                     "description": 'Flashcards and a quiz. Use when the user says "hazme tarjetas de", "examíname", "repasemos".'},
                    {"name": "hoard-daily-digest", "description": 'The day in one page. Use when "resumen del día".'}]

    monkeypatch.setattr("services.memory.skills.SkillsManager", _Skills)
    view = '{"action": "view", "name": "hoard-study-cards"}'
    assert gate.allows("manage_skills", view, "Examíname con las tarjetas pendientes del mazo Faustus.")
    # another skill, or a write, keeps the gate
    assert not gate.allows("manage_skills", '{"action": "view", "name": "hoard-daily-digest"}', "Examíname.")
    assert not gate.allows("manage_skills", '{"action": "edit", "name": "hoard-study-cards"}', "Examíname.")
    assert not gate.allows("manage_skills", view, "Hola, ¿qué tal?")
