"""Lot X-C: the MCP tools block in the prompt must be scoped to this turn's
selected tools, not a dump of every tool on every connected server.

Confirmed live: a bare "hola" carried a 14,657-token "MCP tools" block
(``src/mcp_manager.py::get_tool_descriptions_for_prompt``) even though the
turn's tool-RAG retrieval already sent the relevant tools' full native
schemas. This block only needs a one-line reminder per selected tool, plus
one line per connected server that had nothing selected.
"""
from __future__ import annotations

import pytest

from src.mcp_manager import McpManager


def _fake_get_setting(budget=1500, full_listing=False):
    values = {
        "agent_mcp_prompt_budget_tokens": budget,
        "agent_mcp_prompt_full_listing": full_listing,
    }

    def _get(key, default=None):
        return values.get(key, default)

    return _get


def _manager_with_servers(n_servers=3, n_tools=20):
    """3 servers x 20 tools = 60 MCP tools, like the live machine's ~10
    servers x ~21 tools that produced the 14,657-token dump."""
    mgr = McpManager()
    for si in range(n_servers):
        server_id = f"server{si}"
        mgr._connections[server_id] = {"name": f"Server {si}"}
        mgr._tools[server_id] = [
            {
                "name": f"tool{ti}",
                "description": f"Does thing {ti} for server {si}." * 3,
                "input_schema": {},
            }
            for ti in range(n_tools)
        ]
    return mgr


class TestScopedBlock:
    def test_only_selected_tools_get_a_line_others_get_a_count(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_setting", _fake_get_setting())
        mgr = _manager_with_servers(3, 20)
        selected = {"mcp__server0__tool0", "mcp__server1__tool5"}

        block = mgr.get_tool_descriptions_for_prompt({}, relevant_tools=selected)

        assert "mcp__server0__tool0" in block
        assert "mcp__server1__tool5" in block
        # Every other tool from server0/server1 must NOT get its own line.
        assert "mcp__server0__tool1:" not in block
        assert "mcp__server1__tool6:" not in block
        # server2 had nothing selected: one "more tools" line, not 20 lines.
        assert "Server 2: 20 more tools" in block
        assert "mcp__server2__tool0" not in block
        # server0/server1 are represented, so they get no "more tools" line
        # even though only one of their 20 tools was selected.
        assert "Server 0: 20 more tools" not in block
        assert "Server 1: 20 more tools" not in block
        assert "lookup_tools" in block

    def test_block_is_capped_at_the_budget(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_setting", _fake_get_setting(budget=1500))
        mgr = _manager_with_servers(3, 20)
        # Select nearly everything, so the naive one-line-per-tool block
        # would blow well past 1500 tokens if it were not capped.
        selected = {
            f"mcp__server{si}__tool{ti}"
            for si in range(3) for ti in range(20)
        }

        block = mgr.get_tool_descriptions_for_prompt({}, relevant_tools=selected)
        estimated_tokens = len(block) / 4
        assert estimated_tokens <= 1500

    def test_full_listing_setting_restores_old_behaviour(self, monkeypatch):
        monkeypatch.setattr(
            "src.settings.get_setting", _fake_get_setting(full_listing=True)
        )
        mgr = _manager_with_servers(3, 20)
        selected = {"mcp__server0__tool0"}

        block = mgr.get_tool_descriptions_for_prompt({}, relevant_tools=selected)

        # Every tool of every server shows up, not just the selected one.
        for si in range(3):
            for ti in range(20):
                assert f"mcp__server{si}__tool{ti}:" in block
        assert "more tools" not in block

    def test_budget_zero_disables_the_block_entirely(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_setting", _fake_get_setting(budget=0))
        mgr = _manager_with_servers(3, 20)
        selected = {"mcp__server0__tool0"}

        block = mgr.get_tool_descriptions_for_prompt({}, relevant_tools=selected)
        assert block == ""

    def test_relevant_tools_none_is_always_the_full_dump(self, monkeypatch):
        """index_mcp_tools (tool-RAG's indexer) always calls this with no
        relevant_tools — it must keep seeing every tool no matter how the
        settings scope the live prompt, or lookup_tools/tool-RAG would go
        blind to whatever the prompt stopped listing."""
        monkeypatch.setattr("src.settings.get_setting", _fake_get_setting(budget=1500))
        mgr = _manager_with_servers(3, 20)

        block = mgr.get_tool_descriptions_for_prompt({})
        for si in range(3):
            for ti in range(20):
                assert f"mcp__server{si}__tool{ti}:" in block


class TestLookupToolsStillFindsMcpTools:
    def test_lookup_tools_indexes_off_the_full_dump_not_the_scoped_prompt(self, monkeypatch, tmp_path):
        """lookup_tools reads the tool-RAG index (src/tool_index.py), which
        is built from get_tool_descriptions_for_prompt(relevant_tools=None) —
        i.e. never affected by the scoped-prompt settings above."""
        import src.embedding_lanes as lanes
        import src.tool_index as ti
        import src.tool_index_memory as tim
        from tests.test_tool_index_memory_lane import HashingEmbedder, _chroma_down

        _chroma_down(monkeypatch)
        monkeypatch.setattr(lanes, "_build_fastembed_client", lambda: HashingEmbedder())
        monkeypatch.setattr(tim, "DEFAULT_CACHE_PATH", str(tmp_path / "cache.json"))
        monkeypatch.setattr(
            "src.settings.get_setting",
            _fake_get_setting(budget=1500, full_listing=False),
        )

        mgr = _manager_with_servers(1, 3)

        index = ti.ToolIndex()
        index.index_builtin_tools()
        index.index_mcp_tools(mgr)

        row_ids = {row["id"] for row in index.corpus_rows()}
        assert "mcp__server0__tool0" in row_ids
        assert "mcp__server0__tool1" in row_ids
        assert "mcp__server0__tool2" in row_ids


class TestIntegrationsBlock:
    def test_names_only_when_api_call_not_selected(self):
        from src.integrations import get_integrations_prompt

        import src.integrations as integ_mod

        def _fake_load():
            return [
                {"id": "gitea", "name": "Gitea", "description": "x" * 500, "enabled": True},
                {"id": "linkding", "name": "Linkding", "description": "y" * 500, "enabled": True},
            ]

        orig = integ_mod.load_integrations
        integ_mod.load_integrations = _fake_load
        try:
            block = get_integrations_prompt(relevant_tools={"some_other_tool"})
        finally:
            integ_mod.load_integrations = orig

        assert "Gitea" in block
        assert "Linkding" in block
        assert "x" * 500 not in block
        estimated_tokens = len(block) / 4
        assert estimated_tokens <= 400

    def test_full_descriptions_when_api_call_selected(self):
        from src.integrations import get_integrations_prompt
        import src.integrations as integ_mod

        def _fake_load():
            return [{"id": "gitea", "name": "Gitea", "description": "does git things", "enabled": True}]

        orig = integ_mod.load_integrations
        integ_mod.load_integrations = _fake_load
        try:
            block = get_integrations_prompt(relevant_tools={"api_call"})
        finally:
            integ_mod.load_integrations = orig

        assert "does git things" in block


class TestContextLedgerClassification:
    def test_mcp_and_integrations_get_their_own_section(self):
        from src.context_ledger import classify

        mcp_msg = {"role": "user", "content": "x" * 10,
                   "metadata": {"trusted": False, "source": "MCP tools"}}
        integ_msg = {"role": "user", "content": "x" * 10,
                     "metadata": {"trusted": False, "source": "integrations"}}
        assert classify(mcp_msg) == "mcp"
        assert classify(integ_msg) == "integrations"

    def test_new_sections_count_as_retrieved_for_advice(self):
        from src.context_ledger import _RETRIEVED
        assert "mcp" in _RETRIEVED
        assert "integrations" in _RETRIEVED
