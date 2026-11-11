"""Strict native tool schemas use managed endpoint metadata and preserve batches."""
import copy
import json

import httpx
import pytest

from src import engine_controls as controls, llm_core


BASE = 'http://managed.invalid/v1'
MODEL = 'fixture-model'
TOOLS = [{'type': 'function', 'function': {
    'name': 'read_file', 'parameters': {
        'type': 'object', 'properties': {'path': {'type': 'string'}, 'limit': {'type': 'integer'}},
        'required': ['path'], 'additionalProperties': False,
    },
}}]


def document():
    return {'paths': {'/v1/chat/completions': {'post': {'requestBody': {'content': {
        'application/json': {'schema': {'$ref': '#/components/schemas/Request'}}}}}}},
        'components': {'schemas': {
            'Request': {'type': 'object', 'properties': {
                'tools': {'anyOf': [{'type': 'array', 'items': {'$ref': '#/components/schemas/Tool'}}, {'type': 'null'}]}}},
            'Tool': {'type': 'object', 'properties': {'function': {'$ref': '#/components/schemas/Function'}}},
            'Function': {'type': 'object', 'properties': {
                'name': {'type': 'string'}, 'parameters': {}, 'strict': {'type': 'boolean', 'default': False}}},
        }}}


@pytest.fixture
def managed(monkeypatch):
    monkeypatch.setattr(controls, '_schema_cache', {})
    monkeypatch.setattr(llm_core, 'is_sparks_url', lambda url: url.startswith(BASE))
    monkeypatch.setattr(llm_core, '_endpoint_id_for_url', lambda url: 'managed')
    monkeypatch.setattr(llm_core, '_structured_output_enabled', lambda: True)
    controls.refresh_json_schema([{'endpoint_id': 'managed', 'base_url': BASE, 'models': [MODEL]}],
                                 fetch_openapi=lambda _: document())


def test_typed_route_enables_strict_tools_independently_of_text_format(managed):
    capability = controls.json_schema_capability(BASE, 'managed')
    assert capability['strict_tools'] is True
    assert capability['json_schema'] is False
    payload = {'tools': copy.deepcopy(TOOLS), 'parallel_tool_calls': True}
    original = copy.deepcopy(payload)
    llm_core._apply_strict_tool_schemas(payload, BASE)
    assert payload['tools'][0]['function']['strict'] is True
    assert payload['tools'][0]['function']['parameters'] == TOOLS[0]['function']['parameters']
    assert payload['parallel_tool_calls'] is True
    assert 'tool_choice' not in payload
    assert 'strict' not in original['tools'][0]['function']
    assert 'strict' not in TOOLS[0]['function']


@pytest.mark.parametrize('mutation', ['wrong_type', 'missing', 'different_route', 'unreferenced'])
def test_requires_strict_boolean_on_actual_request(mutation):
    doc = document()
    function = doc['components']['schemas']['Function']['properties']
    if mutation == 'wrong_type':
        function['strict'] = {'type': 'string'}
    elif mutation == 'missing':
        function.pop('strict')
    elif mutation == 'different_route':
        doc['paths']['/other'] = doc['paths'].pop('/v1/chat/completions')
    else:
        doc['components']['schemas']['Tool']['properties']['function'] = {'type': 'object'}
    assert controls._openapi_strict_tools(doc, BASE) is False


@pytest.mark.parametrize('doc', [None, [], {}, {'paths': None}, {'paths': []},
    {'paths': {'/v1/chat/completions': None}},
    {'paths': {'/v1/chat/completions': {'post': {'requestBody': {'content': None}}}}}])
def test_malformed_metadata_is_unverified(doc):
    assert controls._openapi_strict_tools(doc, BASE) is False


@pytest.mark.parametrize('mode', ['off', 'unmanaged', 'expired', 'retired', 'changed', 'outage'])
def test_unverified_or_disabled_endpoints_preserve_schemas(managed, monkeypatch, mode):
    if mode == 'off':
        monkeypatch.setattr(llm_core, '_structured_output_enabled', lambda: False)
    elif mode == 'unmanaged':
        monkeypatch.setattr(llm_core, 'is_sparks_url', lambda _: False)
    elif mode == 'expired':
        controls._schema_cache[('managed', BASE)]['at'] -= 61
    elif mode == 'retired':
        controls.refresh_json_schema([], fetch_openapi=lambda _: pytest.fail('no probe'))
    else:
        controls._schema_cache[('managed', BASE)]['at'] -= 61
        def unavailable(_):
            raise OSError('offline')
        controls.refresh_json_schema([{'endpoint_id': 'managed', 'base_url': BASE,
            'models': ['changed-model' if mode == 'changed' else MODEL]}], fetch_openapi=unavailable)
    payload = {'tools': copy.deepcopy(TOOLS)}
    llm_core._apply_strict_tool_schemas(payload, BASE)
    assert payload == {'tools': TOOLS}


def test_explicit_strict_choice_and_optional_arguments_survive(managed):
    tools = copy.deepcopy(TOOLS)
    tools[0]['function']['strict'] = False
    payload = {'tools': tools}
    llm_core._apply_strict_tool_schemas(payload, BASE)
    assert payload['tools'] == tools
    assert payload['tools'][0]['function']['parameters']['required'] == ['path']


@pytest.mark.asyncio
async def test_normal_stream_sends_strict_and_assembles_three_calls(managed, monkeypatch):
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        chunks = []
        for index, name in enumerate(['alpha', 'beta', 'gamma']):
            for part in [dict(name='read_file', arguments='{"path":'), dict(arguments=json.dumps(name)+'}')]:
                chunks.append('data: ' + json.dumps({'choices': [{'delta': {'tool_calls': [{
                    'index': index, 'id': f'call_{index}', 'type': 'function', 'function': part}]}}]}))
        chunks.append('data: '+json.dumps({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]}))
        chunks.append('data: [DONE]')
        return httpx.Response(200, text='\n\n'.join(chunks)+'\n\n')
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(llm_core, '_get_http_client', lambda: client)
        monkeypatch.setattr(llm_core, '_is_host_dead', lambda _: False)
        monkeypatch.setattr(llm_core, '_is_self_hosted_openai_compatible', lambda _: True)
        monkeypatch.setattr(llm_core, '_is_local_ollama_target', lambda _: False)
        monkeypatch.setattr(llm_core, '_sparks_initial_wait', lambda _: None)
        monkeypatch.setattr(llm_core, 'note_model_activity', lambda *a, **k: None)
        events = []
        async for chunk in llm_core._stream_llm_inner(BASE, MODEL,
                [{'role': 'user', 'content': 'Read three files independently.'}], tools=TOOLS):
            for line in chunk.splitlines():
                if line.startswith('data: ') and line != 'data: [DONE]':
                    events.append(json.loads(line[6:]))
    assert len(requests) == 1
    assert requests[0]['parallel_tool_calls'] is True
    assert requests[0]['tools'][0]['function']['strict'] is True
    calls = next(event['calls'] for event in events if event.get('type') == 'tool_calls')
    assert [json.loads(call['arguments'])['path'] for call in calls] == ['alpha', 'beta', 'gamma']
