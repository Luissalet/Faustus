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
