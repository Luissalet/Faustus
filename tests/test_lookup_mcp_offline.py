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


def test_a_panel_turn_finds_its_own_app_tools_first(monkeypatch):
    rename_deck = 'mcp__ac3e1027__deck_update'
    schemas = [
        {'type': 'function', 'function': {'name': 'mcp__writer__project_rename',
         'description': '[MCP:Writer] Rename a project: change its title.', 'parameters': {'type': 'object', 'properties': {}}}},
        {'type': 'function', 'function': {'name': 'mcp__kafka__document_rename',
         'description': '[MCP:Kafka] Rename a document title.', 'parameters': {'type': 'object', 'properties': {}}}},
        {'type': 'function', 'function': {'name': 'mcp__links__link_rename',
         'description': '[MCP:Links] Rename a saved link title.', 'parameters': {'type': 'object', 'properties': {}}}},
        {'type': 'function', 'function': {'name': rename_deck,
         'description': '[MCP:Cicero] Update deck settings: theme, language, metadata.',
         'parameters': {'type': 'object', 'properties': {'deck_id': {'type': 'string'}, 'title': {'type': 'string'}}}}},
        {'type': 'function', 'function': {'name': 'mcp__ac3e1027__slide_add',
         'description': '[MCP:Cicero] Add a slide to a deck.', 'parameters': {'type': 'object', 'properties': {}}}},
    ]
    class Manager:
        def get_all_openai_schemas(self, ctx): return schemas
    monkeypatch.setattr('src.tool_utils.get_mcp_manager', lambda: Manager())
    monkeypatch.setattr('src.tool_index.get_tool_index', lambda: None)
    monkeypatch.setattr('src.tool_security.owner_is_admin_or_single_user', lambda owner: True)
    query = '{"query":"rename title of the presentation deck","k":3}'
    plain = tool_serve.execute_lookup(query, {'disabled_tools': ()})[1]['promote']
    from src import hoard_copilot_turn as turn
    token = turn._ORIGIN_SERVER.set('ac3e1027')
    try:
        panel = tool_serve.execute_lookup(query, {'disabled_tools': ()})[1]['promote']
        disabled = tool_serve.execute_lookup(query, {'disabled_tools': (rename_deck,)})[1]['promote']
    finally:
        turn._ORIGIN_SERVER.reset(token)
    assert panel[0] == rename_deck
    assert rename_deck not in disabled
    assert rename_deck not in plain  # what a lookup from the Cicero panel got before
    assert len(panel) <= 4


def test_inflected_words_still_find_the_tool(monkeypatch):
    update = 'mcp__898369d2__wh_update_writing'
    schemas = [
        {'type': 'function', 'function': {'name': 'mcp__898369d2__wh_pin_reference',
         'description': '[MCP:writer] Pin or unpin a codex entry for a chapter (or the whole book) in the reference panel.',
         'parameters': {'type': 'object', 'properties': {}}}},
        {'type': 'function', 'function': {'name': 'mcp__898369d2__wh_set_coverage_cell',
         'description': '[MCP:writer] Set a manual character x chapter coverage role. Does not edit the manuscript.',
         'parameters': {'type': 'object', 'properties': {}}}},
        {'type': 'function', 'function': {'name': update,
         'description': '[MCP:writer] Change a manuscript piece; replacing content snapshots the old version first. Passing content REPLACES the whole body.',
         'parameters': {'type': 'object', 'properties': {}}}},
    ]
    class Manager:
        def get_all_openai_schemas(self, ctx): return schemas
    monkeypatch.setattr('src.tool_utils.get_mcp_manager', lambda: Manager())
    monkeypatch.setattr('src.tool_index.get_tool_index', lambda: None)
    monkeypatch.setattr('src.tool_security.owner_is_admin_or_single_user', lambda owner: True)
    from src import hoard_copilot_turn as turn
    token = turn._ORIGIN_SERVER.set('898369d2')
    try:
        found = tool_serve.execute_lookup('{"query":"edit or replace text in a writing chapter","k":5}', {'disabled_tools': ()})[1]['promote']
    finally:
        turn._ORIGIN_SERVER.reset(token)
    assert update in found[:3]
    assert tool_serve._stemmed('Replacing REPLACES replace writes writing') == 'replac replac replac writ writ'
