"""MCP tools are embedded again only when their indexed text changes.

Seen live: a chat's own browser instance disconnecting at the end of every
turn moved the manager's generation, and each move dropped and re-embedded
all 475 MCP tools in the background (6-29 s) while the next turn prepared."""
from __future__ import annotations

from tests.test_tool_index_memory_lane import _chroma_down, _use_embedder


class Mgr:
    def __init__(self, tools):
        self._generation = 1
        self.tools = tools

    def _grouped_prompt_tools(self, disabled):
        return {"Hoard": [{"qualified_name": n, "description": d} for n, d in self.tools.items()]}

    def get_tool_descriptions_for_prompt(self, disabled):
        return ""


def _index(monkeypatch, tmp_path):
    _chroma_down(monkeypatch)
    _use_embedder(monkeypatch, tmp_path)
    from src.tool_index import ToolIndex

    index = ToolIndex()
    encoded = []
    for lane in index._lanes:
        real = lane.encode

        def counting(texts, _real=real):
            encoded.append(len(texts))
            return _real(texts)
        monkeypatch.setattr(lane, "encode", counting)
    return index, encoded


def test_a_generation_bump_with_the_same_tools_embeds_nothing(monkeypatch, tmp_path):
    index, encoded = _index(monkeypatch, tmp_path)
    mgr = Mgr({"mcp__h__a": "read the calendar", "mcp__h__b": "send a note"})
    index.index_mcp_tools(mgr)
    assert encoded == [2]
    mgr._generation += 1
    index.index_mcp_tools(mgr)
    assert encoded == [2]
    assert {row["id"] for row in index.corpus_rows()} >= {"mcp__h__a", "mcp__h__b"}


def test_only_the_changed_tool_is_embedded_and_a_removed_one_is_dropped(monkeypatch, tmp_path):
    index, encoded = _index(monkeypatch, tmp_path)
    mgr = Mgr({"mcp__h__a": "read the calendar", "mcp__h__b": "send a note"})
    index.index_mcp_tools(mgr)
    mgr.tools = {"mcp__h__a": "read the calendar and the agenda", "mcp__h__c": "list files"}
    mgr._generation += 1
    index.index_mcp_tools(mgr)
    assert encoded == [2, 2]
    stored = index._collection.get(where={"tool_type": "mcp"})
    assert sorted(stored["ids"]) == ["mcp_mcp__h__a", "mcp_mcp__h__c"]
