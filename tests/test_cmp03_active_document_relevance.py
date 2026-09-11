"""CMP-03 (seen live): a selection sent from the document panel is the user
pointing at the document, and a Spanish "reescribe este párrafo" is as much
a document edit as the English "rewrite this paragraph". Before this the
relevance gate (`_turn_targets_active_document`) was English-only, judged the
turn unrelated, and `suggest_document` failed with "No active document"."""
from src.agent_loop import _turn_targets_active_document


class _Doc:
    current_content = "# Notas\n\nLa costa se vacía."
    title = "Notas del mar"
    language = "markdown"


def _rel(text, domains=()):
    return _turn_targets_active_document({"domains": set(domains)}, text, _Doc())


def test_naming_a_document_tool_or_the_selection_targets_the_document():
    assert _rel("Reescribe solo el fragmento seleccionado y propón el cambio con suggest_document")
    assert _rel("aclara esta selección")
    assert _rel("usa edit_document para cambiar el título")


def test_spanish_edit_verbs_with_a_text_unit_target_the_document():
    assert _rel("corrige el segundo párrafo")
    assert _rel("mejora esta frase")
    assert _rel("acorta la introducción")


def test_unrelated_turns_still_leave_the_document_alone():
    assert not _rel("quién soy")
    assert not _rel("busca noticias de hoy")
    assert not _rel("qué hora es en Tokio")


def test_no_document_is_never_relevant():
    assert not _turn_targets_active_document({"domains": {"documents"}}, "corrige el párrafo", None)
