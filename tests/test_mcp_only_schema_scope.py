from src.tool_policy import ToolPolicy, MCP_ONLY_HELPERS, known_tool_names


def test_mcp_only_visibility_matches_execution_scope():
    policy=ToolPolicy(mode='mcp_only')
    hidden=policy.all_disabled_names()
    assert {'glob','read_file','python','bash','delegate_agents'} <= hidden
    assert not (MCP_ONLY_HELPERS & hidden)
    for name in known_tool_names():
        assert (name in hidden)==policy.blocks(name)
    assert not policy.blocks('mcp__cicero__asset_import')


def test_normal_visibility_and_explicit_mcp_denials_are_preserved():
    assert ToolPolicy(disabled_tools=frozenset({'glob'})).all_disabled_names()=={'glob'}
    policy=ToolPolicy(mode='mcp_only',disabled_tools=frozenset({'mcp__cicero__deck_export'}))
    assert 'mcp__cicero__deck_export' in policy.all_disabled_names()
