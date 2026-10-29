"""A tuple-shaped tool argument (draft-07 `items: [a, b]`) must reach the model
as `prefixItems`: one such tool in a server's catalogue made a strict endpoint
reject every request of the turn."""

from src.mcp_manager import llm_safe_tool_schema


TUPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "array",
                "items": [{"type": "number", "minimum": 0}, {"type": "number", "exclusiveMinimum": 0}],
                "minItems": 2,
                "maxItems": 2,
            },
            "maxItems": 10,
        },
        "url": {"type": "string"},
    },
    "required": ["url"],
}


def test_tuple_items_become_prefix_items():
    out = llm_safe_tool_schema(TUPLE_SCHEMA)
    inner = out["properties"]["sections"]["items"]
    assert inner["prefixItems"] == [{"type": "number", "minimum": 0}, {"type": "number", "exclusiveMinimum": 0}]
    assert "items" not in inner
    assert inner["minItems"] == 2 and inner["maxItems"] == 2
    assert out["properties"]["sections"]["maxItems"] == 10
    assert out["required"] == ["url"]


def test_original_schema_is_not_mutated():
    before = repr(TUPLE_SCHEMA)
    llm_safe_tool_schema(TUPLE_SCHEMA)
    assert repr(TUPLE_SCHEMA) == before


def test_ordinary_schemas_come_back_equal():
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"type": "string"}},
            "items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}}}},
            "mode": {"enum": ["a", "b"], "default": "a"},
            "either": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
        },
    }
    assert llm_safe_tool_schema(schema) == schema


def test_additional_items_follow_a_tuple():
    out = llm_safe_tool_schema({"type": "array", "items": [{"type": "string"}], "additionalItems": False})
    assert out == {"type": "array", "prefixItems": [{"type": "string"}], "items": False}


def test_values_that_look_like_schemas_are_left_alone():
    schema = {"type": "object", "properties": {"x": {"type": "array", "default": [{"items": [1, 2]}], "examples": [{"items": [3]}]}}}
    assert llm_safe_tool_schema(schema) == schema


def test_discovered_tools_keep_one_schema_everywhere():
    """The catalogue, the step snapshot and the tool search all read the stored
    schema: it is normalised once, where the tool is discovered, so they agree."""
    import asyncio
    import mcp.types as types
    from src import mcp_tool_cache
    from src.mcp_manager import McpManager

    class Session:
        async def list_tools(self, cursor=None):
            return types.ListToolsResult(
                tools=[types.Tool(name="media_download", description="d", inputSchema=TUPLE_SCHEMA)], nextCursor=None)

    async def go():
        mgr = McpManager()
        tools, _ = await mgr._discover_tools_paginated(Session(), "tuple-app", "1.0.0")
        return tools

    mcp_tool_cache.clear()
    try:
        tools = asyncio.run(go())
    finally:
        mcp_tool_cache.clear()
    stored = tools[0]["input_schema"]
    assert "prefixItems" in stored["properties"]["sections"]["items"]
    assert stored == llm_safe_tool_schema(TUPLE_SCHEMA)
