"""Managed recipe sampling is native unless a caller/model explicitly pins it."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src import llm_core, settings
import src.database as database


LOCAL = {
    'local_temperature_default': .6, 'agent_local_temperature_cap': .4,
    'local_top_p_default': .8, 'local_top_k_default': 20,
    'local_repeat_penalty_default': 1.05, 'local_min_p_default': .05,
}
SAMPLERS = {'top_p', 'top_k', 'min_p', 'repeat_penalty', 'repetition_penalty'}


@pytest.fixture(autouse=True)
def no_unrelated_embedding(monkeypatch):
    from src import tool_index
    monkeypatch.setattr(tool_index, 'get_tool_index', lambda: None)
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')


@pytest.fixture
def managed(monkeypatch):
    """Real endpoint identity lookup, entirely within an independent SQLite DB."""
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    database.ModelEndpoint.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, 'SessionLocal', sessions)
    monkeypatch.setattr(settings, 'load_settings', lambda: {'sparks_endpoints': {'recipe-a': 'recipe-endpoint'}})
    monkeypatch.setattr(settings, 'get_setting', lambda key, default=None: LOCAL.get(key, default))
    llm_core._response_cache.clear()

    def register(url):
        with sessions() as db:
            db.merge(database.ModelEndpoint(id='recipe-endpoint', name='Own recipe', base_url=url,
                                            endpoint_kind='local', is_enabled=True))
            db.commit()
        assert llm_core.is_sparks_url(url)
        if url.endswith('/v1'):
            assert llm_core.is_sparks_url(url.rstrip('/') + '/chat/completions')
        return url
    yield register
    engine.dispose()


def test_managed_floor_keeps_server_temperature(managed):
    url = managed('http://127.0.0.1:39001/v1')
    assert llm_core.local_temperature_floor(url, False) is None
    assert llm_core.local_temperature_floor(url, True) is None


@pytest.mark.parametrize('explicit', [{}, {'top_p': .95, 'top_k': -1, 'min_p': 0.0},
                                     {'repeat_penalty': 1.0, 'top_p': .75}])
def test_only_explicit_sampling_and_existing_output_cap(managed, explicit):
    url = managed('http://127.0.0.1:39001/v1')
    payload = {'temperature': 1.0, **explicit}
    automatic = llm_core._apply_local_generation_stability(payload, url, 'arbitrary-model')
    assert {k: payload[k] for k in SAMPLERS & payload.keys()} == explicit
    assert payload['temperature'] == 1.0
    assert payload['max_tokens'] == 8192  # sampler exemption does not remove output fallback
    assert automatic is False


@pytest.mark.parametrize('budget', [0, -1, 321])
def test_explicit_output_budget_unchanged(managed, budget):
    url = managed('http://127.0.0.1:39001/v1')
    payload = {'temperature': .9}
    if budget > 0: payload['max_tokens'] = budget
    llm_core._apply_local_generation_stability(payload, url, 'arbitrary-model', requested_max_tokens=budget)
    assert payload == {'temperature': .9, **({'max_tokens': budget} if budget > 0 else {})}


def test_unmanaged_local_is_byte_equivalent_to_previous_defaults(managed):
    managed('http://127.0.0.1:39001/v1')
    payload = {'temperature': .7}
    assert llm_core._apply_local_generation_stability(payload, 'http://127.0.0.1:39002/v1', 'arbitrary-model') is True
    assert payload == {'temperature': .7, 'repeat_penalty': 1.05, 'min_p': .05,
                       'top_p': .8, 'top_k': 20, 'max_tokens': 8192}
    assert llm_core.local_temperature_floor('http://127.0.0.1:39002/v1', False) == .6


@contextmanager
def private_model(fail_status=None):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append({'path': self.path, 'body': body})
            if fail_status:
                raw = json.dumps({'error': {'message': 'Private fixture rejected request'}}).encode()
                self.send_response(fail_status); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
                return
            if body.get('stream'):
                rows = [
                    {'choices': [{'delta': {'role': 'assistant'}, 'finish_reason': None}]},
                    {'choices': [{'delta': {'reasoning_content': 'Considered.'}, 'finish_reason': None}]},
                    {'choices': [{'delta': {'content': 'READY'}, 'finish_reason': None}]},
                    {'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
                    {'choices': [], 'usage': {'prompt_tokens': 3, 'completion_tokens': 4}},
                ]
                raw = (''.join('data: ' + json.dumps(row) + '\n\n' for row in rows) + 'data: [DONE]\n\n').encode()
                kind = 'text/event-stream'
            else:
                raw = json.dumps({'choices': [{'message': {'content': 'READY'}, 'finish_reason': 'stop'}],
                                  'usage': {'prompt_tokens': 3, 'completion_tokens': 4}}).encode()
                kind = 'application/json'
            self.send_response(200); self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield 'http://127.0.0.1:' + str(server.server_port) + '/v1', received
    finally: server.shutdown(); server.server_close(); thread.join(2)


@pytest.mark.parametrize('mode', ['sync', 'async', 'stream'])
@pytest.mark.parametrize('explicit', [False, True])
def test_actual_tcp_payload_all_entrypoints(managed, mode, explicit):
    with private_model() as (url, received):
        managed(url)
        messages = [{'role': 'user', 'content': 'Respond READY'}]
        overrides = {'top_p': .95, 'top_k': -1, 'min_p': 0.0, 'repeat_penalty': 1.0} if explicit else None
        if mode == 'sync':
            # The synchronous public API has no gen_overrides argument.
            assert llm_core.llm_call(url, 'arbitrary-model', messages, temperature=.9, max_tokens=321, timeout=5) == 'READY'
        elif mode == 'async':
            assert asyncio.run(llm_core.llm_call_async(url, 'arbitrary-model', messages,
                                temperature=.9, max_tokens=321, timeout=5, gen_overrides=overrides)) == 'READY'
        else:
            async def consume():
                return [chunk async for chunk in llm_core.stream_llm(url, 'arbitrary-model', messages,
                            temperature=.9, max_tokens=321, timeout=5, gen_overrides=overrides)]
            chunks = asyncio.run(consume())
            assert any('READY' in chunk for chunk in chunks)
            assert any('Considered.' in chunk for chunk in chunks)
            assert any('stop' in chunk for chunk in chunks)
        assert len(received) == 1
        request = received[0]['body']
        assert request['temperature'] == .9 and request['max_tokens'] == 321
        assert {k: request[k] for k in SAMPLERS & request.keys()} == (overrides if explicit and mode != 'sync' else {})


@pytest.mark.parametrize('explicit,initial', [(False, 1.0), (True, .9)])
def test_actual_agent_loop_keeps_managed_temperature(monkeypatch, tmp_path, managed, explicit, initial):
    from tests.test_agent_local_temperature_cap_scope import _capture_loop
    url = managed('http://127.0.0.1:39001/v1')
    temperature, info = _capture_loop(monkeypatch, tmp_path,
        [{'role': 'user', 'content': 'Fix src/parser.py and run its pytest.'}], url, initial, explicit)
    assert temperature == initial
    assert info['temperature_cap'] is None and info['temperature_capped_from'] is None


def test_saved_model_options_remain_explicit_without_generic_native_floors(managed, monkeypatch):
    from src import model_load_options, gpu_policy
    url = managed('http://127.0.0.1:39001/api/chat')
    saved = {'top_p': .93, 'extra': {'temperature': .77, 'min_p': 0.0}}
    monkeypatch.setattr(model_load_options, 'resolve_for_request', lambda *args: saved)
    monkeypatch.setattr(gpu_policy, 'preferred_main_gpu', lambda *args: None)
    defaults = llm_core._model_load_defaults(url, 'arbitrary-model')
    assert defaults == saved
    merged = llm_core._with_model_defaults(url, 'arbitrary-model', {'top_p': .89})
    payload = {'options': {'temperature': 1.0}}
    llm_core._apply_gen_overrides_ollama(payload, merged, url=url)
    assert payload == {'options': {'temperature': .77, 'top_p': .89, 'min_p': 0.0}}


def test_managed_model_name_does_not_activate_legacy_implicit_preset(managed):
    url = managed('http://127.0.0.1:39001/v1')
    payload = {'temperature': .9}
    llm_core._apply_local_generation_stability(payload, url, 'minimax-private-fixture')
    assert payload == {'temperature': .9, 'max_tokens': 2048}


@pytest.mark.parametrize('direction', ['to-managed', 'from-managed'])
@pytest.mark.parametrize('coding', [False, True])
@pytest.mark.parametrize('explicit', [False, True])
def test_mixed_agent_fallbacks_over_actual_tcp(monkeypatch, tmp_path, managed, direction, coding, explicit):
    import src.agent_loop as loop
    monkeypatch.setattr(loop, 'get_mcp_manager', lambda: None)
    monkeypatch.setattr(loop, 'estimate_tokens', lambda *a, **k: 10)
    monkeypatch.setattr(loop, 'blocked_tools_for_owner', lambda owner: set())
    monkeypatch.setattr(loop, 'get_setting', lambda key, default=None: LOCAL.get(key, default))
    # Exercise the real ordered-fallback implementation, with no HTTP retry.
    async def provider(*args, **kwargs):
        async for chunk in llm_core.stream_llm_with_fallback(*args, **kwargs, max_retries=0):
            yield chunk
    monkeypatch.setattr(loop, 'stream_llm_with_fallback', provider)
    with private_model(400) as (primary, first), private_model() as (backup, second):
        managed(backup if direction == 'to-managed' else primary)
        initial = .9 if explicit else 1.0
        user = 'Fix src/parser.py and run pytest.' if coding else 'Hello'
        async def run():
            return [chunk async for chunk in loop.stream_agent_loop(primary, 'arbitrary-model',
                [{'role': 'user', 'content': user}], temperature=initial, temperature_explicit=explicit,
                max_rounds=1, workspace=str(tmp_path), context_length=32768, relevant_tools=set(),
                fallbacks=[(backup, 'arbitrary-model', None)], fallback_statuses=[400],
                harness_options={'checkpoints': False, 'run_tests': False, 'repo_map': False})]
        chunks = asyncio.run(run())
        # Context compaction may also send native tokenizer metadata to these
        # fixtures; sampling assertions apply to the actual generation POSTs.
        first = [row for row in first if row['path'].endswith('/chat/completions')]
        second = [row for row in second if row['path'].endswith('/chat/completions')]
        assert first and second, ''.join(chunks)
        ordinary_temperature = .9 if explicit else .4 if coding else .6
        expected = [ordinary_temperature, initial] if direction == 'to-managed' else [initial, ordinary_temperature]
        for rows, temperature in zip((first, second), expected):
            assert all(row['body']['temperature'] == temperature for row in rows)
        managed_rows = second if direction == 'to-managed' else first
        ordinary_rows = first if direction == 'to-managed' else second
        assert all(not (SAMPLERS & row['body'].keys()) for row in managed_rows)
        assert all(row['body'].get('min_p') == .05 and row['body'].get('top_p') == .8 for row in ordinary_rows)


@pytest.mark.parametrize('direction', ['to-managed', 'from-managed', 'ordinary'])
@pytest.mark.parametrize('explicit', [False, True])
def test_plain_chat_candidate_temperature_is_route_local(monkeypatch, managed, direction, explicit):
    import routes.chat_routes as chat
    primary, backup = 'http://127.0.0.1:39001/v1', 'http://127.0.0.1:39002/v1'
    managed(backup if direction == 'to-managed' else primary if direction == 'from-managed'
            else 'http://127.0.0.1:39003/v1')
    async def compact(session, url, model, messages, headers, **kwargs):
        return messages, 32768, False
    monkeypatch.setattr(chat, 'maybe_compact', compact)
    monkeypatch.setattr(chat, 'trim_for_context', lambda messages, context: messages)
    original = .9 if explicit else 1.0
    factory, _ = chat._chat_candidate_request_factory([{'role': 'user', 'content': 'Hello'}],
        session=SimpleNamespace(endpoint_url=primary), temperature=original, temperature_explicit=explicit)
    async def prepare():
        return [await factory(i, url, 'arbitrary-model', None) for i, url in enumerate((primary, backup))]
    requests = asyncio.run(prepare())
    if direction == 'ordinary':
        assert all('kwargs' not in request for request in requests)
    else:
        managed_index = 1 if direction == 'to-managed' else 0
        assert requests[managed_index]['kwargs']['temperature'] == original
        ordinary = requests[1 - managed_index]
        if 'kwargs' in ordinary: assert ordinary['kwargs']['temperature'] == (.9 if explicit else .6)


@pytest.mark.asyncio
async def test_plain_chat_native_route_preserves_caller_default(monkeypatch, managed):
    from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint
    import routes.chat_routes as chat
    captured = {}
    url = managed('http://127.0.0.1:39001/v1')
    endpoint = _chat_stream_endpoint(monkeypatch, 'chat', captured, endpoint_url=url)
    monkeypatch.setattr(settings, 'get_setting', lambda key, default=None: LOCAL.get(key, default))
    seen = []
    async def provider(candidates, messages, **kwargs):
        seen.append(kwargs['temperature'])
        yield 'data: {"delta":"READY"}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(chat, 'stream_llm_with_fallback', provider)
    response = await endpoint(_RouteRequest('chat'))
    async for chunk in response.body_iterator: pass
    assert seen == [.2]  # fixture's caller value; never hard-code T1 for Sparks
