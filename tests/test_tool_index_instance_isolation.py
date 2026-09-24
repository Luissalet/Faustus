"""PENDIENTES 23-09 noche — a shared ChromaDB collection across instances.

All Faustus instances (different ports / data dirs) used the SAME
``odysseus_tool_index_fastembed`` collection on the same Chroma server, each
with its own tool catalogue. `index_mcp_tools` deletes EVERY row of
``tool_type == "mcp"`` in that shared collection (not only its own) before
re-adding its own set -- so instance A reindexing its MCP tools erased
instance B's MCP tools until B happened to reindex again too.

The fix: `tool_index.instance_collection_name()` namespaces the collection
per instance, by default from a stable hash of this instance's own data
directory (`src.constants.DATA_DIR`), or explicitly via the
`tool_index_collection_suffix` setting. This proves two instances sharing
one fake Chroma server, with different MCP catalogues, no longer interfere:
each keeps its own collection, and neither instance's reindex touches the
other's rows.
"""

import asyncio

from src.mcp_manager import McpManager
from src.tool_index import ToolIndex
import src.tool_index as tool_index_module

from tests.helpers.embedding_lanes import FakeChroma, FakeEmbedder, patch_chroma


def _mgr_with_tool(server_id: str, tool_name: str, description: str) -> McpManager:
    mgr = McpManager()
    mgr._tools[server_id] = [{
        "name": tool_name,
        "description": description,
        "input_schema": {"type": "object", "properties": {}},
    }]
    mgr._connections[server_id] = {"status": "connected", "name": server_id}
    mgr._generation = 1
    return mgr


def _make_index(monkeypatch, suffix: str) -> ToolIndex:
    """One "Faustus instance"'s tool index: its own collection name, its own
    fastembed lane, sharing whatever Chroma client is currently patched in."""
    monkeypatch.setattr(tool_index_module, "_instance_collection_suffix", lambda: suffix)
    monkeypatch.setattr(
        tool_index_module._lanes_mod, "_build_fastembed_client",
        lambda: FakeEmbedder(384, "mini", "local://fastembed"),
    )
    return ToolIndex()


def test_two_instances_get_different_collection_names(monkeypatch):
    fake = FakeChroma()
    patch_chroma(monkeypatch, fake)

    index_a = _make_index(monkeypatch, "instance_a")
    index_b = _make_index(monkeypatch, "instance_b")

    index_a.index_builtin_tools()
    index_b.index_builtin_tools()

    names = set(fake.collections.keys())
    assert "odysseus_tool_index_instance_a_fastembed" in names
    assert "odysseus_tool_index_instance_b_fastembed" in names
    # Never the old shared, unsuffixed name -- nothing writes there any more.
    assert "odysseus_tool_index_fastembed" not in names


def test_reindexing_mcp_tools_on_one_instance_does_not_erase_the_other(monkeypatch):
    fake = FakeChroma()
    patch_chroma(monkeypatch, fake)

    index_a = _make_index(monkeypatch, "instance_a")
    index_b = _make_index(monkeypatch, "instance_b")

    mgr_a = _mgr_with_tool("srv_alpha", "tool_alpha", "Reads the alpha catalogue")
    mgr_b = _mgr_with_tool("srv_beta", "tool_beta", "Writes to the beta ledger")

    index_a.index_mcp_tools(mgr_a)
    index_b.index_mcp_tools(mgr_b)

    assert "mcp__srv_alpha__tool_alpha" in index_a.retrieve("alpha catalogue", k=10)
    assert "mcp__srv_beta__tool_beta" in index_b.retrieve("beta ledger", k=10)

    # A reconnects with a DIFFERENT set of MCP tools (its catalogue changed) --
    # this is exactly what live triggered the shared-collection bug: a fresh
    # index_mcp_tools() call that used to wipe every "mcp" row in the shared
    # collection, instance B's included.
    mgr_a2 = _mgr_with_tool("srv_alpha", "tool_alpha_v2", "The alpha tool's new name")
    mgr_a2._generation = 2
    index_a.index_mcp_tools(mgr_a2)

    assert "mcp__srv_alpha__tool_alpha_v2" in index_a.retrieve("alpha tool new name", k=10)
    assert "mcp__srv_alpha__tool_alpha" not in index_a.retrieve("alpha tool new name", k=10)
    # B's tool must still be exactly where B left it.
    assert "mcp__srv_beta__tool_beta" in index_b.retrieve("beta ledger", k=10)

    # Prove it at the storage layer too, not just through retrieval: B's
    # collection was never touched by A's reindex.
    b_collection_name = "odysseus_tool_index_instance_b_fastembed"
    assert any(
        row["metadata"].get("tool_name") == "mcp__srv_beta__tool_beta"
        for row in fake.collections[b_collection_name].rows.values()
    )
    a_collection_name = "odysseus_tool_index_instance_a_fastembed"
    assert "mcp__srv_beta__tool_beta" not in {
        row["metadata"].get("tool_name")
        for row in fake.collections[a_collection_name].rows.values()
    }


def test_same_tool_name_on_both_instances_does_not_overwrite_the_others_content(monkeypatch):
    """Two instances whose catalogues both use a plain `mcp_<name>` id (the
    real id scheme -- no server/instance in it) must not silently overwrite
    each other's document on upsert once they are in separate collections."""
    fake = FakeChroma()
    patch_chroma(monkeypatch, fake)

    index_a = _make_index(monkeypatch, "instance_a")
    index_b = _make_index(monkeypatch, "instance_b")

    mgr_a = _mgr_with_tool("srv_shared_name", "sync_data", "Instance A's own sync_data tool")
    mgr_b = _mgr_with_tool("srv_shared_name", "sync_data", "Instance B's own, DIFFERENT sync_data tool")

    index_a.index_mcp_tools(mgr_a)
    index_b.index_mcp_tools(mgr_b)

    row_id = "mcp_mcp__srv_shared_name__sync_data"
    a_doc = fake.collections["odysseus_tool_index_instance_a_fastembed"].rows[row_id]["document"]
    b_doc = fake.collections["odysseus_tool_index_instance_b_fastembed"].rows[row_id]["document"]
    assert "Instance A's own" in a_doc
    assert "Instance B's own, DIFFERENT" in b_doc


def test_explicit_suffix_setting_overrides_the_data_dir_hash(monkeypatch):
    monkeypatch.setattr(tool_index_module, "get_setting", None, raising=False)  # not used directly
    from src import settings as settings_module

    monkeypatch.setattr(
        settings_module, "get_setting",
        lambda key, default=None: "pinned-name" if key == "tool_index_collection_suffix" else default,
    )
    assert tool_index_module.instance_collection_name() == "odysseus_tool_index_pinned_name"


def test_no_override_and_no_data_dir_keeps_the_legacy_shared_name(monkeypatch):
    from src import settings as settings_module

    monkeypatch.setattr(
        settings_module, "get_setting",
        lambda key, default=None: default,
    )
    import src.constants as constants_module
    monkeypatch.setattr(constants_module, "DATA_DIR", "", raising=False)
    assert tool_index_module.instance_collection_name() == tool_index_module.COLLECTION_NAME
