"""Structured request contracts and actual loopback HTTP; no models or Hub."""
import asyncio
import copy
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

import httpx
import pytest

from src import engine_controls, llm_core as core

SCHEMA = {'type': 'object', 'properties': {'categoría': {'type': 'string', 'enum': ['código', 'consulta']},
          'válido': {'type': 'boolean'}}, 'required': ['categoría', 'válido'], 'additionalProperties': False}
OPENAPI = json.loads((Path(__file__).parent / 'fixtures/sparks-json-schema-openapi.json').read_text(encoding='utf-8'))


@pytest.fixture(autouse=True)
def clean_schema_cache():
    engine_controls._schema_cache.clear()
    yield
    engine_controls._schema_cache.clear()


@pytest.fixture
def http_backend():
    class Wire(list): pass
    requests = Wire()
    requests.gets = []
    requests.document = copy.deepcopy(OPENAPI)
    requests.answer = {'categoría': 'código', 'válido': True}
    requests.status = 200
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            requests.gets.append(self.path)
            data = json.dumps(requests.document).encode()
            self.send_response(200 if self.path == '/openapi.json' else 404)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            value = {'model': 'future-model', 'choices': [{'message': {'content': json.dumps(requests.answer, ensure_ascii=False)}, 'finish_reason': 'stop'}],
                     'usage': {'prompt_tokens': 20, 'completion_tokens': 12}}
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(requests.status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.fixture
def managed(monkeypatch, http_backend):
    from src import model_backend
    url, requests = http_backend
    core._response_cache.clear()
    monkeypatch.setattr(core, 'is_sparks_url', lambda base: base == url)
    monkeypatch.setattr(core, '_endpoint_id_for_url', lambda base: 'schema-fixture' if base == url else None)
    monkeypatch.setattr(engine_controls, 'json_schema_capability', lambda base, endpoint_id: {'json_schema': base == url and endpoint_id == 'schema-fixture'}, raising=False)
    monkeypatch.setattr(model_backend, 'serving_backend', lambda *a, **k: {'backend': 'unknown'})
    monkeypatch.setattr(core, '_model_load_defaults', lambda *a: {})
    monkeypatch.setattr(core, '_ollama_model_caps', lambda *a: None)
    monkeypatch.setattr(core, '_is_host_dead', lambda *a: False)
    monkeypatch.setattr(core, 'note_model_activity', lambda *a: None)
    monkeypatch.setattr(core, '_schedule_heal_on_garbage', lambda *a: None)
    @asynccontextmanager
    async def slot(*args): yield
    monkeypatch.setattr(core, '_local_model_slot', slot)
    return url, requests


@pytest.mark.parametrize('mode', ['sync', 'async'])
def test_managed_verified_sparks_schema_reaches_real_wire(monkeypatch, managed, mode):
    url, requests = managed
    messages = [{'role': 'user', 'content': 'Clasifica el texto y devuelve JSON.'}]
    if mode == 'sync':
        result = core.llm_call(url, 'future-model', messages, response_schema=SCHEMA, max_tokens=128, timeout=5)
    else:
        async def run():
            async with httpx.AsyncClient(trust_env=False) as client:
                monkeypatch.setattr(core, '_get_http_client', lambda: client)
                return await core.llm_call_async(url, 'future-model', messages, response_schema=SCHEMA, max_tokens=128, timeout=5)
        result = asyncio.run(run())
    assert json.loads(result) == {'categoría': 'código', 'válido': True}
    assert len(requests) == 1
    assert requests[0].get('response_format') == {'type': 'json_schema', 'json_schema': {
        'name': 'response', 'strict': True, 'schema': SCHEMA}}
    assert requests[0]['model'] == 'future-model' and requests[0]['max_tokens'] == 128
    assert 'tools' not in requests[0]


def test_real_metadata_refresh_and_generation_cache_reads(http_backend):
    url, requests = http_backend
    rows = [{'endpoint_id': 'configured', 'base_url': url, 'models': ['future-model']}]
    result = engine_controls.refresh_json_schema(rows)
    assert result['configured']['json_schema'] is True
    for _ in range(20):
        assert engine_controls.json_schema_capability(url, 'configured')['json_schema'] is True
        assert engine_controls.refresh_json_schema(rows)['configured']['json_schema'] is True
    assert requests.gets == ['/openapi.json']
    assert not engine_controls.json_schema_capability(url, 'different-id')['json_schema']
    assert not engine_controls.json_schema_capability(url + '/other', 'configured')['json_schema']
    rows[0]['models'] = ['another-model']
    engine_controls.refresh_json_schema(rows)
    assert requests.gets == ['/openapi.json', '/openapi.json']
    engine_controls.refresh_json_schema([])
    assert not engine_controls.json_schema_capability(url, 'configured')['json_schema']


@pytest.mark.parametrize('mutation', ['name_type', 'schema_type', 'strict_type', 'selector_type', 'enum_types',
    'wrong_route', 'missing_ref', 'cycle', 'ref_type_sibling', 'union_type_sibling',
    'required_type', 'unknown_required', 'schema_not_arbitrary_object', 'name_const',
    'schema_enum', 'strict_const'])
def test_malformed_or_unsupported_contract_is_not_verified(mutation):
    doc = copy.deepcopy(OPENAPI)
    components = doc['components']['schemas']
    envelope = components['JsonSchemaResponseFormat']['properties']
    fmt = components['ResponseFormat']['properties']
    if mutation == 'name_type': envelope['name'] = {'type': 'integer'}
    elif mutation == 'schema_type': envelope['schema'] = {'type': 'string'}
    elif mutation == 'strict_type': envelope['strict'] = {'type': 'string'}
    elif mutation == 'selector_type': fmt['type']['type'] = 'boolean'
    elif mutation == 'enum_types': fmt['type']['enum'].append(True)
    elif mutation == 'wrong_route': doc['paths']['/other'] = doc['paths'].pop('/v1/chat/completions')
    elif mutation == 'missing_ref': fmt['json_schema'] = {'$ref': '#/components/schemas/Absent'}
    elif mutation == 'cycle': fmt['json_schema'] = {'$ref': '#/components/schemas/ResponseFormat'}
    elif mutation == 'ref_type_sibling': fmt['json_schema'] = {'$ref': '#/components/schemas/JsonSchemaResponseFormat', 'type': 'string'}
    elif mutation == 'union_type_sibling': fmt['json_schema']['type'] = 'string'
    elif mutation == 'required_type': components['JsonSchemaResponseFormat']['required'] = True
    elif mutation == 'unknown_required': components['ResponseFormat']['required'] = ['type', 'unprovided']
    elif mutation == 'schema_not_arbitrary_object': envelope['schema'] = {'type': 'object', 'additionalProperties': False}
    elif mutation == 'name_const': envelope['name']['const'] = 'unusable-name'
    elif mutation == 'schema_enum': envelope['schema'] = {'type': 'object', 'enum': [{}]}
    elif mutation == 'strict_const': envelope['strict'] = {'type': 'boolean', 'const': False}
    assert not engine_controls._openapi_json_schema(doc, 'http://fixture.invalid/v1'), mutation


def test_negative_cache_expiry_identity_url_and_bound(monkeypatch):
    clock, fetches = [10.0], []
    monkeypatch.setattr(engine_controls.time, 'monotonic', lambda: clock[0])
    def missing(base): fetches.append(base); raise TimeoutError('fixture metadata failure')
    rows = [{'endpoint_id': 'configured', 'base_url': 'http://fixture.invalid/v1', 'models': ['m']}]
    for _ in range(20):
        result = engine_controls.refresh_json_schema(rows, fetch_openapi=missing)
        assert result['configured']['json_schema'] is False and result['configured']['status'] == 'unavailable'
    assert len(fetches) == 1
    clock[0] += engine_controls._SCHEMA_TTL + 1
    assert engine_controls.json_schema_capability(rows[0]['base_url'], 'configured')['status'] == 'unverified'
    result = engine_controls.refresh_json_schema(rows, fetch_openapi=lambda base: OPENAPI)
    assert result['configured']['json_schema'] is True
    rows[0]['base_url'] = 'http://changed.invalid/v1'
    engine_controls.refresh_json_schema(rows, fetch_openapi=lambda base: OPENAPI)
    assert not engine_controls.json_schema_capability('http://fixture.invalid/v1', 'configured')['json_schema']
    many = [{'endpoint_id': str(i), 'base_url': 'http://fixture.invalid/v1', 'models': ['m']} for i in range(engine_controls._SCHEMA_MAX_ENDPOINTS + 1)]
    engine_controls.refresh_json_schema(many, fetch_openapi=lambda base: OPENAPI)
    assert len(engine_controls._schema_cache) == engine_controls._SCHEMA_MAX_ENDPOINTS


def test_changed_model_invalidates_capability_before_metadata_is_fetched():
    rows = [{'endpoint_id': 'configured', 'base_url': 'http://fixture.invalid/v1', 'models': ['old']}]
    engine_controls.refresh_json_schema(rows, fetch_openapi=lambda base: OPENAPI)
    rows[0]['models'] = ['new']
    def observe_during_fetch(base):
        assert engine_controls.json_schema_capability(base, 'configured')['status'] == 'unverified'
        return OPENAPI
    assert engine_controls.refresh_json_schema(rows, fetch_openapi=observe_during_fetch)['configured']['json_schema']


@pytest.mark.parametrize('effort', ['high', 'max'])
@pytest.mark.parametrize('budget', [0, -1, 128])
def test_schema_does_not_suppress_sparks_reasoning(managed, effort, budget):
    url, _ = managed
    payload = {'model': 'glm-5.3-flash', 'reasoning_effort': effort,
               'chat_template_kwargs': {'enable_thinking': True, 'clear_thinking': False},
               'thinking_token_budget': budget}
    original = copy.deepcopy(payload)
    assert core._apply_openai_response_format(payload, url, SCHEMA, model=payload['model'])
    payload.pop('response_format')
    assert payload == original


def test_tools_setting_off_and_unconfigured_endpoints_stay_unchanged(monkeypatch, managed):
    from src import settings
    url, _ = managed
    payload = {'model': 'm'}
    assert not core._apply_openai_response_format(payload, url, SCHEMA, tools=[{'type': 'function'}])
    assert payload == {'model': 'm'}
    monkeypatch.setattr(settings, 'get_setting', lambda key, default=None: 'off' if key == 'local_structured_output' else default)
    assert core._resolve_response_schema(url, SCHEMA) is None
    monkeypatch.setattr(core, 'is_sparks_url', lambda base: False)
    assert core._schema_transport(url) is None


@pytest.fixture
def native_managed(monkeypatch, tmp_path, http_backend):
    """Real native endpoint creation, settings, identity lookup and metadata GET."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import core.database as db
    import src.database as compat_db
    from src import endpoint_resolver, sparks, settings
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    for module in (db, compat_db, endpoint_resolver):
        monkeypatch.setattr(module, 'SessionLocal', factory)
    url, requests = http_backend
    real_request = sparks._request
    def metadata(method, path, body=None, **kwargs):
        if path == '/api/endpoints':
            return 200, {'endpoints': [{'recipe': 'future-recipe', 'base_url': url, 'models': ['future-model']}]}
        if path == '/api/ui/call' and body['name'] == 'recipes_list':
            return 200, {'recipes': [{'name': 'future-recipe'}]}
        assert path == '/openapi.json' and kwargs['url'] == url[:-3]
        return real_request(method, path, body, **kwargs)
    monkeypatch.setattr(sparks, '_request', metadata)
    settings.update_settings({'sparks_enabled': True, 'sparks_default_backend': False,
                              'sparks_endpoints': {}, 'local_structured_output': 'auto'})
    synced = sparks.sync()
    identity = synced['endpoints']['future-recipe']
    assert synced['schema_capabilities'][identity]['json_schema'] is True
    settings.update_settings({'default_endpoint_id': identity, 'default_model': 'future-model',
                              'utility_endpoint_id': identity, 'utility_model': 'future-model'})
    assert core.is_sparks_url(url)
    assert core._schema_transport(url) == 'response_format'
    core._response_cache.clear()
    monkeypatch.setattr(core, '_schedule_heal_on_garbage', lambda *a: None)
    @asynccontextmanager
    async def slot(*args): yield
    monkeypatch.setattr(core, '_local_model_slot', slot)
    yield url, requests, identity
    engine.dispose()


def test_native_sync_to_real_generation_and_identity_removal(native_managed):
    from src import settings
    url, requests, identity = native_managed
    core.llm_call(url, 'future-model', [{'role': 'user', 'content': 'Clasifica esta entrada.'}], response_schema=SCHEMA, timeout=5)
    assert len(requests) == 1 and requests[0]['response_format']['json_schema']['schema'] == SCHEMA
    assert requests.gets == ['/openapi.json']
    settings.update_settings({'sparks_endpoints': {}})
    assert core._schema_transport(url) is None  # cached capability cannot authorize an unmanaged URL


def test_existing_extraction_route_uses_production_core_and_checks_evidence(monkeypatch, native_managed):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.extraction_routes as route
    url, requests, _ = native_managed
    text = 'FACTURA F-42\nTotal: 1234,56 EUR'
    schema = {'type': 'object', 'properties': {'invoice': {'type': 'string'}, 'total': {'type': 'number'}},
              'required': ['invoice', 'total']}
    requests.answer = {'data': {'invoice': 'F-42', 'total': 1234.56}, 'evidence': [
        {'path': 'invoice', 'quote': 'FACTURA F-42', 'unit': 1},
        {'path': 'total', 'quote': 'Total: 1234,56 EUR', 'unit': 1}]}
    monkeypatch.setattr(route, '_owner', lambda request: '')
    app = FastAPI()
    app.include_router(route.setup_extraction_routes())
    with TestClient(app) as client:
        result = client.post('/api/extract', json={'text': text, 'schema': schema})
        assert result.status_code == 200, result.text
        body = result.json()
        assert body['schema_valid'] and body['data'] == {'invoice': 'F-42', 'total': 1234.56}
        assert len(body['evidence']) == 2
        sent = requests[0]['response_format']['json_schema']['schema']
        assert sent['required'] == ['data', 'evidence'] and 'tools' not in requests[0]
        assert requests.gets == ['/openapi.json']
        # A grammar does not establish truth: an unsupported quote must still fail evidence checks.
        requests.answer['evidence'][1]['quote'] = 'Total: 9999 EUR'
        result = client.post('/api/extract', json={'text': text + '\nSegundo documento.', 'schema': schema})
        assert result.status_code == 200
        assert result.json()['schema_valid'] is False and result.json()['data']['total'] is None


@pytest.mark.parametrize('effort,budget', [('high', 0), ('max', -1), ('high', 128)])
def test_async_wire_preserves_reasoning_usage_and_metadata(monkeypatch, managed, effort, budget):
    url, requests = managed
    monkeypatch.setattr(engine_controls, 'vllm_thinking_budget', lambda base: True)
    observed, traces = [], []
    from src import llm_trace
    monkeypatch.setattr(llm_trace, 'record_call', lambda **kw: traces.append(kw))
    async def run():
        async with httpx.AsyncClient(trust_env=False) as client:
            monkeypatch.setattr(core, '_get_http_client', lambda: client)
            return await core.llm_call_async(url, 'glm-5.3-flash', [{'role': 'user', 'content': 'Clasifica el documento técnico.'}],
                response_schema=SCHEMA, timeout=5, max_tokens=1024, return_model_metadata=True,
                gen_overrides={'think': True, 'reasoning_effort': effort, 'reasoning_budget': budget},
                _usage_observer=lambda usage, **kwargs: observed.append(usage))
    result = asyncio.run(run())
    assert json.loads(result[0]) == {'categoría': 'código', 'válido': True}
    assert result[1] == 'future-model'
    assert requests[0]['reasoning_effort'] == effort and requests[0]['thinking_token_budget'] == budget
    assert requests[0]['chat_template_kwargs']['enable_thinking'] is True
    assert requests[0]['response_format']['type'] == 'json_schema'
    assert len(observed) == 1
    assert observed[0]['input_tokens'] == 20 and observed[0]['output_tokens'] == 12
    assert observed[0]['model'] == 'future-model' and observed[0]['requested_model'] == 'glm-5.3-flash'
    assert observed[0]['usage_source'] == 'reported_engine'
    assert traces[-1]['usage'] == observed[0] and traces[-1]['request']['response_schema'] == SCHEMA


@pytest.mark.parametrize('gate', ['off', 'unverified', 'unmanaged'])
def test_ineligible_schema_payload_is_identical_to_free_text(monkeypatch, managed, gate):
    from src import settings
    url, requests = managed
    messages = [{'role': 'user', 'content': 'Clasifica esta entrada técnica.'}]
    core.llm_call(url, 'future-model', messages, timeout=5)
    baseline = requests[-1]
    core._response_cache.clear()
    if gate == 'off':
        original_get = settings.get_setting
        monkeypatch.setattr(settings, 'get_setting', lambda key, default=None: 'off' if key == 'local_structured_output' else original_get(key, default))
    elif gate == 'unverified':
        monkeypatch.setattr(engine_controls, 'json_schema_capability', lambda *a: {'json_schema': False})
    else:
        monkeypatch.setattr(core, 'is_sparks_url', lambda *a: False)
    core.llm_call(url, 'future-model', messages, response_schema=SCHEMA, timeout=5)
    assert len(requests) == 2 and requests[-1] == baseline


def test_http_rejection_remains_failure_and_does_not_retry_without_schema(managed):
    from fastapi import HTTPException
    url, requests = managed
    requests.status = 400
    with pytest.raises(HTTPException):
        core.llm_call(url, 'future-model', [{'role': 'user', 'content': 'Clasifica el contrato.'}],
                      response_schema=SCHEMA, timeout=5)
    assert requests and all(row['response_format']['type'] == 'json_schema' for row in requests)
