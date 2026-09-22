"""Tool-RAG indexes the whole MCP tool description (src/tool_index.py).

Seen live: a plugin whose tools end their descriptions with the words a
person would say in Spanish ("cuánto es, porcentaje, días laborables") was
never retrieved for "cuánto es el 17,5 % de 2.347,80": the indexer parsed
the prompt listing, which keeps 120 characters of each description's first
line, so none of those words were ever indexed. The model reached for web
search and a Python sandbox instead of the exact calculator it had.
"""
from __future__ import annotations

from tests.test_tool_index_memory_lane import _chroma_down, _use_embedder


LONG_DESC = (
    "Evaluate an arithmetic expression exactly, with rationals instead of floats.\n\n"
    "Use it for any number the answer depends on: sums, percentages, ratios.\n"
    "Returns {id, exact, decimal, cite}.\n"
    "Keywords: calculate, percentage, calcular, cuánto es, porcentaje, tanto por ciento"
)


class Mgr:
    """A manager with the structured listing the real McpManager has."""

    def __init__(self, generation=1):
        self._generation = generation

    def _grouped_prompt_tools(self, disabled):
        return {"Laplace's Hoard": [{
            "qualified_name": "mcp__lap__calc",
            "description": LONG_DESC,
            "server_name": "Laplace's Hoard",
        }]}

    def get_tool_descriptions_for_prompt(self, disabled):
        # What the prompt listing really carries: 120 chars of line one.
        first = LONG_DESC[:120] + "..."
        return "**Laplace's Hoard:**\n  - mcp__lap__calc: " + first


def test_words_after_the_first_line_are_indexed(monkeypatch, tmp_path):
    _chroma_down(monkeypatch)
    _use_embedder(monkeypatch, tmp_path)
    from src.tool_index import ToolIndex

    index = ToolIndex()
    index.index_builtin_tools()
    index.index_mcp_tools(Mgr())
    rows = {row["id"]: row for row in index.corpus_rows()}
    assert "mcp__lap__calc" in rows
    text = str(rows["mcp__lap__calc"])
    assert "porcentaje" in text and "cuánto es" in text


def test_without_a_structured_listing_the_prompt_text_is_still_used(monkeypatch, tmp_path):
    _chroma_down(monkeypatch)
    _use_embedder(monkeypatch, tmp_path)
    from src.tool_index import ToolIndex

    class Old:
        _generation = 1

        def get_tool_descriptions_for_prompt(self, disabled):
            return "**home:**\n- turn_on_lights: Turn on the smart lights in a room\n"

    index = ToolIndex()
    index.index_builtin_tools()
    index.index_mcp_tools(Old())
    assert index._lanes[0].collection.get(where={"tool_type": "mcp"})["ids"] == ["mcp_turn_on_lights"]


def test_a_very_long_description_is_capped(monkeypatch, tmp_path):
    from src import tool_index as ti

    class Verbose(Mgr):
        def _grouped_prompt_tools(self, disabled):
            return {"x": [{"qualified_name": "mcp__x__t", "description": "word " * 5000}]}

    docs, ids, metas = ti._mcp_index_docs(Verbose(), {})
    assert ids == ["mcp_mcp__x__t"]
    assert len(docs[0]) <= ti.MCP_INDEX_DESC_CHARS + 60
