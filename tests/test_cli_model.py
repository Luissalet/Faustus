import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from src import cli_model as client, external_worker as worker


def test_answer_is_separate_from_progress_and_cost():
    stream = worker._Stream()
    stream.feed(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Progress'}]}}))
    stream.feed(json.dumps({'type': 'result', 'subtype': 'success', 'result': '# Respuesta\nHola', 'total_cost_usd': 1}))
    assert stream.response_text == '# Respuesta\nHola'


def test_nested_result_does_not_replace_top_level_answer():
    stream = worker._Stream()
    stream.feed(json.dumps({'type': 'result', 'result': 'Main', 'is_error': False, 'session_id': 'main'}))
    stream.feed(json.dumps({'type': 'result', 'result': 'Child', 'is_error': True, 'parent_tool_use_id': 'x', 'session_id': 'child'}))
    assert stream.response_text == 'Main'
    assert stream.result['is_error'] is False and stream.session_id == 'main'


def test_context_retains_roles_tool_results_unicode():
    prompt = client.render_context([{'role': 'system', 'content': 'Español'},
        {'role': 'tool', 'content': [{'type': 'text', 'text': '✓'}], 'tool_call_id': 'abc'}])
    assert 'Español' in prompt and '✓' in prompt and '"tool_call_id": "abc"' in prompt


def test_images_are_rejected_not_silently_discarded():
    with pytest.raises(client.ClientModelError, match='text only'):
        client.render_context([{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:...'}}]}])


@pytest.mark.parametrize('provider,model', [('claude', 'client-default'), ('codex', 'gpt-5')])
def test_official_client_harness_never_sends_native_mcp_schemas(monkeypatch, provider, model):
    from src import agent_loop as al
    from tests.test_agent_harness_loop import _patch_common, _collect
    _patch_common(monkeypatch)
    original = al._build_system_prompt
    schema = {'type': 'function', 'function': {'name': 'mcp_browser_test',
              'description': 'A browser test', 'parameters': {'type': 'object'}}}
    def with_mcp(*args, **kwargs):
        messages, _ = original(*args, **kwargs)
        return messages, [schema]
    monkeypatch.setattr(al, '_build_system_prompt', with_mcp)
    requests = []
    async def capture(candidates, messages, **kwargs):
        request = await kwargs['candidate_request_factory'](0, *candidates[0])
        requests.append(request)
        assert not kwargs.get('tools')
        assert not request['kwargs'].get('tools')
        assert any('## Tool: delegate_agents' in str(m.get('content'))
                   for m in request['messages'])
        yield 'data: {"delta":"Connection verified."}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, 'stream_llm_with_fallback', capture)
    _collect(al.stream_agent_loop(f'faustus-cli://{provider}/subscription/test', model,
        [{'role': 'user', 'content': 'A connection test. Do not browse or call MCP tools.'}],
        max_rounds=1, context_length=128000,
        relevant_tools={'delegate_agents'}, forced_tools={'delegate_agents'}, owner='qa'))
    assert requests


def test_textual_schema_supplement_obeys_selection_and_denials():
    from src.agent_loop import _assemble_prompt
    assert '## Tool: delegate_agents' in _assemble_prompt({'delegate_agents'})
    assert '## Tool: delegate_agents' not in _assemble_prompt({'delegate_agents'}, {'delegate_agents'})
    assert '## Tool: delegate_agents' not in _assemble_prompt({'read_file'})


def test_context_is_bounded(monkeypatch):
    monkeypatch.setattr(client, 'MAX_CONTEXT_CHARS', 10)
    with pytest.raises(client.ClientModelError, match='input limit'):
        client.render_context([{'role': 'user', 'content': 'x' * 20}])


@pytest.fixture
def authorized(monkeypatch):
    monkeypatch.setattr(client, 'authorize', lambda *a: ('claude', 'subscription', 'alice'))


def test_complete_reuses_supervisor_and_disables_native_tools(authorized, monkeypatch):
    def run(runner, prompt, **kwargs):
        assert runner.gate == 'hook'
        assert runner.argv[runner.argv.index('--tools') + 1] == ''
        assert '--strict-mcp-config' in runner.argv
        assert '--disable-slash-commands' in runner.argv
        assert '--no-session-persistence' in runner.argv
        assert kwargs['billing_mode'] == 'subscription'
        assert kwargs['owner'] == 'alice' and kwargs['model'] is None
        assert 'faustus-client-model-' in kwargs['workspace']
        return {'ok': True, 'response_text': 'Hola', 'output_tail': 'do not use diagnostics'}
    monkeypatch.setattr(worker, 'run_task', run)
    assert client.complete('url', 'client-default', [{'role': 'user', 'content': 'Hi'}]) == 'Hola'


@pytest.mark.parametrize('result', [
    {'ok': False, 'error': 'quota exhausted'},
    {'ok': True, 'response_text': ''},
    {'ok': True, 'response_text': 'bad', 'gate': {'stream_tool_calls': 1}},
])
def test_failure_empty_response_or_native_tool_never_accepted(authorized, monkeypatch, result):
    monkeypatch.setattr(worker, 'run_task', lambda *a, **k: result)
    with pytest.raises(client.ClientModelError) as exc:
        client.complete('url', 'model', [])
    assert exc.value.fallback_eligible is False


@pytest.mark.asyncio
async def test_cancellation_waits_for_supervisor_cleanup(monkeypatch):
    entered, stopped = threading.Event(), threading.Event()
    def complete(*a, cancel, **kw):
        entered.set()
        assert cancel.wait(3)
        stopped.set()
        raise client.ClientModelError('cancelled')
    monkeypatch.setattr(client, 'complete', complete)
    task = asyncio.create_task(client.complete_async('url', 'model', []))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_stream_error_explicitly_forbids_paid_fallback(monkeypatch):
    async def fail(*a, **k):
        raise client.ClientModelError('quota exhausted')
    monkeypatch.setattr(client, 'complete_async', fail)
    chunks = [c async for c in client.stream('url', 'model', [])]
    assert len(chunks) == 1 and '"fallback_eligible": false' in chunks[0]
    assert 'quota exhausted' in chunks[0]


def test_url_helpers_do_not_construct_http_endpoints():
    from src.endpoint_resolver import build_chat_url, build_models_url
    url = 'faustus-cli://claude/subscription/abc'
    assert build_chat_url(url) == url
    assert build_models_url(url) is None


@pytest.fixture
def capability_db(monkeypatch):
    import core.database as database
    ep = SimpleNamespace(base_url='faustus-cli://claude/subscription/abc',
        endpoint_kind='official-cli', is_enabled=True, api_key='private-capability',
        pinned_models='["client-default"]', hidden_models='[]', owner='alice')
    class DB:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def query(self, *args): return self
        def filter_by(self, **kwargs):
            self.criteria = kwargs
            return self
        def first(self):
            return ep if all(getattr(ep, k) == v for k,v in self.criteria.items()) else None
    monkeypatch.setattr(database, 'SessionLocal', DB)
    return ep


def test_opaque_capability_and_pinned_model_required(capability_db):
    ep = capability_db
    with pytest.raises(client.ClientModelError): client.authorize(ep.base_url, 'client-default', {})
    headers = {'Authorization': 'Bearer private-capability'}
    assert client.authorize(ep.base_url, 'client-default', headers) == ('claude', 'subscription', 'alice')
    with pytest.raises(client.ClientModelError): client.authorize(ep.base_url, 'other', headers)
    ep.is_enabled = False
    with pytest.raises(client.ClientModelError): client.authorize(ep.base_url, 'client-default', headers)


def test_generic_endpoint_cannot_authorize_a_local_client(capability_db):
    ep = capability_db
    ep.endpoint_kind = 'auto'
    with pytest.raises(client.ClientModelError):
        client.authorize(ep.base_url, 'client-default', {'Authorization': 'Bearer private-capability'})


@pytest.mark.asyncio
async def test_generic_fallback_cannot_switch_away_from_selected_client(monkeypatch):
    from src import llm_core
    calls = []
    async def fake_stream(url, model, messages, **kwargs):
        calls.append(url)
        yield 'event: error\ndata: {"error":"quota exhausted", "status":429,"fallback_eligible":false}\n\n'
    monkeypatch.setattr(llm_core, 'stream_llm', fake_stream)
    url = 'faustus-cli://claude/subscription/abc'
    chunks = [c async for c in llm_core.stream_llm_with_fallback(
        [(url, 'client-default', {}), ('https://api.example.test/v1/chat/completions', 'paid', {})], [])]
    assert calls == [url]
    assert any('quota exhausted' in c for c in chunks)


@pytest.mark.asyncio
async def test_llm_entrypoints_use_client_without_http(monkeypatch):
    from src import llm_core
    monkeypatch.setattr(client, 'complete', lambda *a, **k: 'sync')
    async def complete(*a, **k): return 'async'
    monkeypatch.setattr(client, 'complete_async', complete)
    url = 'faustus-cli://claude/subscription/abc'
    assert llm_core.llm_call(url, 'model', []) == 'sync'
    assert await llm_core.llm_call_async(url, 'model', [], return_model_metadata=True) == ('async', 'model')
    assert any('"delta": "async"' in c for c in [c async for c in llm_core.stream_llm(url, 'model', [])])


def test_private_endpoint_resolves_into_textual_harness_and_revokes(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import core.database as database
    from src import endpoint_resolver, agent_loop
    engine = create_engine('sqlite://', poolclass=StaticPool)
    database.ModelEndpoint.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, 'SessionLocal', sessions)
    monkeypatch.setattr(endpoint_resolver, 'SessionLocal', sessions)
    url = 'faustus-cli://claude/subscription/test'
    try:
        with sessions() as db:
            db.add(database.ModelEndpoint(id='test',name='Claude private',base_url=url,
                api_key='opaque-test-capability',owner='alice',is_enabled=True,
                endpoint_kind='official-cli',model_refresh_mode='manual',supports_tools=False,
                pinned_models='["client-default"]',cached_models='["client-default"]'))
            db.commit()
        assert endpoint_resolver.resolve_endpoint_by_id('test','client-default',owner='bob') is None
        resolved = endpoint_resolver.resolve_endpoint_by_id('test','client-default',owner='alice')
        assert resolved is not None and resolved[:2] == (url,'client-default')
        headers = resolved[2]
        assert client.authorize(url,'client-default',headers) == ('claude','subscription','alice')
        assert agent_loop._agent_route_tool_mode(url,'client-default',owner='alice',headers=headers)[0] is False
        with sessions() as db:
            db.query(database.ModelEndpoint).filter_by(id='test').update({'is_enabled':False})
            db.commit()
        with pytest.raises(client.ClientModelError):
            client.authorize(url,'client-default',headers)
    finally:
        engine.dispose()
