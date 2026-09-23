"""A plugin's MCP tool that changes something is evidence for the answer that
says so.

Seen live on the desktop instance: a turn whose only calls were the model
library plugin's `models_add_root` and `models_stats` ended with "la carpeta
está añadida y el escaneo ya terminó" and the harness rejected it twice as
`claims_without_mutation` — two more rounds of a 27B to repeat a true
sentence — because every `mcp__*` call was recorded as a read.
"""
from src import agent_harness as h


def test_mutating_verbs_in_the_tool_name_make_an_effect():
    for name in (
        "mcp__5bde74bf__models_add_root", "mcp__x__upsert_person", "mcp__x__save_link",
        "mcp__x__add_entry", "mcp__x__model_listing_set", "mcp__x__model_tag",
        "mcp__x__models_rescan", "mcp__x__scribe_start", "mcp__x__library_index",
    ):
        assert h.mcp_tool_looks_mutating(name), name


def test_lookups_stay_reads():
    for name in (
        "mcp__5bde74bf__models_stats", "mcp__x__models_search", "mcp__x__models_dupes",
        "mcp__x__get_person", "mcp__x__list_entries", "mcp__x__screen_recent",
        "mcp__x__library_search", "mcp__x__scribe_sessions", "mcp__x__link_digest",
        "mcp__x__budget_status", "mcp__x__browser_snapshot",
        # not an MCP name at all
        "read_file", "bash", "models_add_root",
    ):
        assert not h.mcp_tool_looks_mutating(name), name


def test_a_true_report_after_a_plugin_mutation_passes(tmp_path):
    ledger = h.TurnLedger(str(tmp_path), "Añade la carpeta Modelos a la biblioteca y dime cuántos hay")
    ledger.record("mcp__5bde74bf__models_add_root", '{"path": "D:/Modelos"}', {"ok": True, "root_id": 1}, 1)
    ledger.record("mcp__5bde74bf__models_stats", "{}", {"models": 13371}, 2)
    assert [e["kind"] for e in ledger.events] == ["effect", "read"]
    check = ledger.check_completion("Listo. La carpeta está añadida a la biblioteca y hay 13.371 modelos.")
    assert "claims_without_mutation" not in check["reasons"]


def test_a_report_after_only_lookups_is_still_rejected(tmp_path):
    ledger = h.TurnLedger(str(tmp_path), "Añade la carpeta Modelos a la biblioteca")
    ledger.record("mcp__5bde74bf__models_stats", "{}", {"models": 0}, 1)
    check = ledger.check_completion("Listo. La carpeta está añadida a la biblioteca.")
    assert "claims_without_mutation" in check["reasons"]


def test_paths_a_plugin_result_cites_are_not_fabricated(tmp_path):
    """The library plugin answered with `api/plugins.md § Plugins`; the model
    repeated that citation and the harness called it a fabricated path
    because nothing in the workspace had that name. A plugin result grounds
    the paths it prints, like `ls` or `grep` do."""
    ledger = h.TurnLedger(str(tmp_path), "hazme tarjetas de lo que dice mi biblioteca sobre plugins")
    ledger.record("mcp__02e5e776__library_search", '{"q": "plugins"}',
                  {"output": '{"hits": [{"file": "api/plugins.md", "page": 1, "text": "A plugin is..."}]}'}, 1)
    ledger.record("mcp__4f9230b5__cards_add", '{"deck": "Faustus", "cards": []}', {"count": 4}, 2)
    check = ledger.check_completion("He creado el mazo Faustus con 4 tarjetas sacadas de api/plugins.md.")
    assert "fabricated_paths" not in check["reasons"], check
    assert "claims_without_mutation" not in check["reasons"], check


def test_record_keeping_claims_need_an_effect(tmp_path):
    """"Te la marco como otra vez" with no tool call is a fabricated grade."""
    for text in (
        "¡Sin problema! Te la marco como \"otra vez\" para que vuelva a salir pronto.",
        "Casi — la he marcado como difícil.",
        "Le he puesto nota de bien y vuelve en un día.",
        "Lo apunto en tu mazo.",
        "I've marked the card as again.",
    ):
        assert h.find_mutation_claims(text), text
    for text in (
        "El más grande es el marco de 226 mm.",
        "Marco Ejemplo es tu vecina.",
        "¿Quieres que la marque como difícil?",
    ):
        assert not h.find_mutation_claims(text), text
    ledger = h.TurnLedger(str(tmp_path), "No me acuerdo.")
    check = ledger.check_completion("Te la marco como otra vez. La respuesta era: conectar y mostrar.")
    assert "claims_without_mutation" in check["reasons"]
    ledger.record("mcp__4f9230b5__card_review", '{"id": 2, "grade": 0}', {"card": {"id": 2}}, 1)
    check = ledger.check_completion("Te la marco como otra vez. La respuesta era: conectar y mostrar.")
    assert "claims_without_mutation" not in check["reasons"]
