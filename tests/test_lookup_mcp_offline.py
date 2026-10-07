"""Connected plugins remain searchable when the embedding index cannot start."""
import pytest

from src import tool_serve
from src.tool_policy import ToolPolicy

INTERPOLATE = 'mcp__studio__studio_interpolate'
EDIT = 'mcp__studio__studio_clip_edit'


@pytest.fixture
def offline(monkeypatch):
    schemas = [
        {'type': 'function', 'function': {'name': INTERPOLATE,
         'description': '[MCP:Studio] Smooth an existing clip to a higher frame rate. Keywords: interpolate, fps, interpolar, fotogramas.',
         'parameters': {'type': 'object', 'properties': {'asset_id': {'type': 'string'}, 'fps': {'type': 'number'}}}}},
        {'type': 'function', 'function': {'name': EDIT,
         'description': '[MCP:Studio] Edit an existing video clip from a prompt. Keywords: redraw, replace, editar video.',
         'parameters': {'type': 'object', 'properties': {}}}},
    ]
    class Manager:
        def get_all_openai_schemas(self, ctx): return schemas
    monkeypatch.setattr('src.tool_utils.get_mcp_manager', lambda: Manager())
    monkeypatch.setattr('src.tool_index.get_tool_index', lambda: None)
    monkeypatch.setattr('src.tool_security.owner_is_admin_or_single_user', lambda owner: True)
    return schemas


@pytest.mark.parametrize('query', ['interpolate video asset fps', 'interpolar fotogramas', 'smooth frame rate'])
def test_natural_query_loads_live_mcp_schema_without_server_label(offline, query):
    payload = tool_serve.serve(query=query, k=1)
    assert payload['promote'] == [INTERPOLATE]
    assert payload['tools'][0]['schema']['function']['parameters']['properties']['fps']['type'] == 'number'


def test_denied_mcp_is_filtered_before_limit(offline):
    payload = tool_serve.serve(query='interpolate video clip', k=1,
                             tool_policy=ToolPolicy(disabled_tools=frozenset({INTERPOLATE})))
    assert payload['promote'] == [EDIT]
    assert INTERPOLATE not in str(payload)


def test_disconnected_mcp_is_not_retained(offline):
    assert tool_serve.serve(query='interpolar fotogramas', k=1)['promote'] == [INTERPOLATE]
    offline.clear()
    assert INTERPOLATE not in tool_serve.serve(query='interpolar fotogramas', k=1)['promote']


def test_unmatched_query_does_not_offer_a_plugin(offline):
    assert tool_serve.serve(query='zxqvnomatch9286', k=1)['tools'] == []


def test_cicero_category_filters_offline_export_search_and_honors_disabled(monkeypatch):
    export = 'mcp__cicero__deck_export'
    unrelated = 'mcp__cicero__cancel_download'
    other_server = 'mcp__studio__deck_export'
    schemas = [
        {'type': 'function', 'function': {'name': export,
         'description': '[MCP:Cicero] Export a presentation deck to PowerPoint PPTX and save the file.',
         'parameters': {'type': 'object', 'properties': {'deck_id': {'type': 'string'}}}}},
        {'type': 'function', 'function': {'name': unrelated,
         'description': '[MCP:Cicero] Cancel a pending asset download.',
         'parameters': {'type': 'object', 'properties': {}}}},
        {'type': 'function', 'function': {'name': other_server,
         'description': '[MCP:Studio] Export a video deck to a file.',
         'parameters': {'type': 'object', 'properties': {}}}},
    ]
    class Manager:
        def get_all_openai_schemas(self, ctx): return schemas
    monkeypatch.setattr('src.tool_utils.get_mcp_manager', lambda: Manager())
    monkeypatch.setattr('src.tool_index.get_tool_index', lambda: None)
    monkeypatch.setattr('src.tool_security.owner_is_admin_or_single_user', lambda owner: True)

    unscoped = tool_serve.serve(query='export deck presentation to PPTX file', k=8)
    assert other_server in unscoped['promote']

    desc, result = tool_serve.execute_lookup(
        '{"query":"export deck presentation to PPTX file","category":"mcp:cicero","k":8}',
        {'disabled_tools': ()},
    )
    assert result['promote'] == [export]
    assert 'schema' in result[tool_serve.LOOKUP_TOOL]['tools'][0]
    assert unrelated not in desc and other_server not in desc

    _desc, disabled_result = tool_serve.execute_lookup(
        '{"query":"export deck presentation to PPTX file","category":"mcp:cicero","k":8}',
        {'disabled_tools': (export,)},
    )
    assert disabled_result['promote'] == []


def test_category_filter_does_not_resolve_an_unconnected_server(offline):
    payload = tool_serve.serve(
        query='interpolate video asset fps', category='mcp:missing', k=8,
    )
    assert payload['promote'] == []
