"""TOOL-02 — deterministic minimum for file-edit intent survives a failed
semantic ranking (acceptance: a Spanish request to edit a .txt file keeps
read/write access even when the retrieval lane finds nothing).
"""
from __future__ import annotations

from src.tool_index import ToolIndex


def _index_with_no_retrieval() -> ToolIndex:
    """A ToolIndex whose semantic retrieval always comes back empty — the
    exact "ranking failed" scenario the acceptance case names — without
    building real embedding lanes (get_tools_for_query touches nothing else
    on the instance)."""
    idx = ToolIndex.__new__(ToolIndex)
    idx.retrieve = lambda query, k=8: []  # simulate a dead/empty ranking lane
    return idx


def test_spanish_file_edit_request_keeps_read_write_when_ranking_fails():
    idx = _index_with_no_retrieval()
    tools = idx.get_tools_for_query("¿puedes editar un archivo .txt para mí?", k=8)
    assert "read_file" in tools
    assert "write_file" in tools


def test_english_file_edit_request_keeps_read_write_when_ranking_fails():
    idx = _index_with_no_retrieval()
    tools = idx.get_tools_for_query("please update the config.py file", k=8)
    assert "read_file" in tools
    assert "write_file" in tools


def test_unrelated_query_does_not_force_file_tools():
    idx = _index_with_no_retrieval()
    tools = idx.get_tools_for_query("what's the weather like today", k=8)
    assert "read_file" not in tools
    assert "write_file" not in tools


def test_file_extension_alone_without_a_verb_does_not_force_file_tools():
    """The gate is deliberately conjunctive (verb AND extension) — a bare
    mention of an extension in passing (e.g. inside a sentence about
    something else) should not drag file tools into every turn."""
    idx = _index_with_no_retrieval()
    tools = idx.get_tools_for_query("what does .txt stand for?", k=8)
    assert "read_file" not in tools
    assert "write_file" not in tools
