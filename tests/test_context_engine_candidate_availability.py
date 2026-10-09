"""Availability probes share retrieval's deadline without freezing the event loop."""
import asyncio
import threading
import time

import pytest

from src.context_engine import candidates as C
from src.context_engine.contracts import ContextRequest


class Source:
    source_id = 'synthetic-slow-availability'
    sections = ()

    def __init__(self, delay=0, search_delay=0, event=None):
        self.delay, self.search_delay, self.event = delay, search_delay, event
        self.calls = 0

    def available(self):
        self.calls += 1
        if self.event:
            self.event.wait(0.3)
        else:
            time.sleep(self.delay)
        return True

    async def search(self, request):
        await asyncio.sleep(self.search_delay)
        return ()


def req():
    return C.RetrievalRequest(request=ContextRequest())


async def test_availability_does_not_block_heartbeat_or_api_handler():
    source = Source(delay=0.3)
    start = time.monotonic()
    job = asyncio.create_task(C.gather([source], req(), timeout_s=0.025))
    await asyncio.sleep(0.005)
    # This coroutine is also what handles another request on the app event loop.
    assert time.monotonic() - start < 0.15
    result = await job
    assert result[0].degraded and 'availability' in result[0].error


async def test_availability_and_search_share_one_budget():
    source = Source(delay=0.045, search_delay=0.06)
    result = await C.gather([source], req(), timeout_s=0.075)
    assert result[0].degraded and 'timeout' in result[0].error


async def test_timed_out_probe_is_reused_and_completion_audited():
    event = threading.Event()
    source = Source(event=event)
    try:
        jobs = [C.gather([source], req(), timeout_s=0.02) for _ in range(6)]
        rows = await asyncio.gather(*jobs)
        assert all(row[0].degraded for row in rows)
        assert source.calls == 1
        assert C.availability_diagnostics()['pending'] >= 1
    finally:
        event.set()
    for _ in range(100):
        if C.availability_diagnostics()['late_completions']:
            break
        await asyncio.sleep(0.005)
    assert C.availability_diagnostics()['late_completions'] >= 1
    assert not (await C.gather([source], req(), timeout_s=0.1))[0].degraded


async def test_cancellation_does_not_kill_or_duplicate_probe():
    event = threading.Event()
    source = Source(event=event)
    job = asyncio.create_task(C.gather([source], req(), timeout_s=1))
    try:
        await asyncio.sleep(0.015)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        second = asyncio.create_task(C.gather([source], req(), timeout_s=0.02))
        assert (await second)[0].degraded
        assert source.calls == 1
    finally:
        event.set()


async def test_inflight_capacity_is_bounded(monkeypatch):
    monkeypatch.setattr(C, 'MAX_AVAILABILITY_PROBES', 3)
    event = threading.Event()
    sources = [Source(event=event) for _ in range(8)]
    try:
        rows = await C.gather(sources, req(), timeout_s=0.02)
        assert sum(source.calls for source in sources) <= 3
        assert any('capacity' in row.error for row in rows)
    finally:
        event.set()


async def test_section_mismatch_never_probes_availability():
    source = Source()
    source.sections = ('code_map',)
    row = (await C.gather([source], C.RetrievalRequest(request=ContextRequest(), sections=('recent_messages',))))[0]
    assert source.calls == 0 and row.error == 'sections not requested'


async def test_unused_gated_source_never_initializes_optional_dependencies():
    source = Source()
    source._gate = lambda request: 'not requested'
    row = (await C.gather([source], req()))[0]
    assert source.calls == 0 and not row.candidates


async def test_exact_fetch_availability_is_bounded_and_off_eventloop():
    source = Source(delay=0.3)
    source.handles = ('qa:',)
    source.fetch = lambda *args: asyncio.sleep(0)
    started = time.monotonic()
    assert await C.fetch_ref('qa:one', req(), sources=[source], timeout_s=0.02) is None
    assert time.monotonic() - started < 0.15


async def test_exact_fetch_shares_budget_and_checks_ref_gate_before_probe():
    source = Source(delay=0.045)
    source.handles = ('qa:',)
    async def fetch(*args):
        await asyncio.sleep(0.06)
        return C.make_candidate(source_ref='qa:one', source_type='memory', section='retrieved_memory', body='synthetic')
    source.fetch = fetch
    assert await C.fetch_ref('qa:one', req(), sources=[source], timeout_s=0.075) is None
    blocked = Source()
    blocked.handles = ('qa:',)
    blocked._ref_gate = lambda request: 'private recall disabled'
    blocked.fetch = fetch
    assert await C.fetch_ref('qa:one', req(), sources=[blocked], timeout_s=0.1) is None
    assert blocked.calls == 0


async def test_probe_preserves_contextvars_and_does_not_mix_inflight_scopes():
    from contextvars import ContextVar
    flag = ContextVar('qa_availability_scope', default=False)
    source = Source(delay=0.04)
    def available():
        time.sleep(0.04)
        return flag.get()
    source.available = available
    async def compile_in_scope(value):
        token = flag.set(value)
        try:
            return (await C.gather([source], req(), timeout_s=0.2))[0]
        finally:
            flag.reset(token)
    denied, permitted = await asyncio.gather(compile_in_scope(False), compile_in_scope(True))
    assert denied.error == 'unavailable'
    assert permitted.error == '' and not permitted.degraded


async def test_live_history_is_available_in_probe_worker():
    from src.context_engine.adapters.sessions import SessionSource, history_scope, history_provider, set_history_provider
    from src.context_engine.contracts import ContextExecution
    previous = history_provider()
    set_history_provider(None)
    try:
        with history_scope('qa-session', 'alice', [{'role': 'user', 'content': 'Live synthetic transcript'}]):
            request = C.RetrievalRequest(request=ContextRequest(execution=ContextExecution(owner='alice', session_id='qa-session')))
            row = (await C.gather([SessionSource()], request, timeout_s=0.2))[0]
            assert row.candidates and row.candidates[0].body == 'Live synthetic transcript'
    finally:
        set_history_provider(previous)


async def test_native_context_api_and_health_remain_responsive(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from routes import context_engine_routes as routes
    from src.context_engine import compiler

    source = Source(delay=0.3)
    source.source_id = 'provenance'
    source.sections = ('past_experiences',)
    monkeypatch.setattr(compiler, '_COMPILER', compiler.ContextCompiler(sources=[source]))
    monkeypatch.setattr(routes, '_owner', lambda request: 'qa-availability')
    monkeypatch.setattr(routes, 'require_admin', lambda request: None)
    app = FastAPI()
    app.include_router(routes.setup_context_engine_routes())

    @app.get('/api/health')
    async def health():
        return {'status': 'healthy'}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://qa') as client:
        compiling = asyncio.create_task(client.post('/api/context/compile', json={
            'task': {'intent': 'code_change', 'phase': 'verify', 'query': 'synthetic'},
        }))
        start = time.monotonic()
        await asyncio.sleep(0.005)
        assert (await client.get('/api/health')).json()['status'] == 'healthy'
        diagnostic = await client.get('/api/context/diagnostics')
        assert diagnostic.status_code == 200, diagnostic.text
        assert time.monotonic() - start < 0.15
        assert 'availability' in diagnostic.json()['compiler']
        assert (await compiling).status_code == 200


def test_diagnostics_contains_bounded_aggregate_counts_only():
    from src.context_engine import compiler
    info = compiler.diagnostics(owner='qa')['availability']
    assert 0 <= info['running'] <= info['workers']
    assert 0 <= info['pending'] <= info['capacity']
    assert all(isinstance(value, int) for value in info.values())


async def test_graph_node_without_owner_remains_in_the_requested_graph_scope(monkeypatch):
    from src import provenance_graph as graph
    from src.context_engine.adapters.provenance import ProvenanceSource, clear_cache
    from src.context_engine.contracts import ContextExecution, ContextPolicy
    owners = []
    def build(owner, **kwargs):
        owners.append(owner)
        return {'nodes': [{'id': 'checkpoint:synthetic', 'kind': 'checkpoint', 'label': 'synthetic', 'meta': {}}], 'edges': []}
    monkeypatch.setattr(graph, 'build', build)
    monkeypatch.setattr(graph, 'ranking_signal', lambda graph: {})
    monkeypatch.setattr(graph, 'explain', lambda *args: {})
    clear_cache()
    request = C.RetrievalRequest(request=ContextRequest(execution=ContextExecution(owner='fixed-owner'), policy=ContextPolicy(allow_personal_memory=False)))
    try:
        rows = await ProvenanceSource().search(request)
        assert owners == ['fixed-owner'] and rows[0].owner == 'fixed-owner'
    finally:
        clear_cache()


@pytest.mark.parametrize('value', ['false', 'true', 0, 1, None, [], {}])
def test_native_mcp_policy_accepts_only_boolean_values(monkeypatch, value):
    import jsonschema
    from mcp_servers import context_engine_server as server
    from src.context_engine.contracts import ContractError
    monkeypatch.setattr(server, '_engine', {'ContextRequest': ContextRequest})
    schema = next(tool.inputSchema for tool in asyncio.run(server.list_tools()) if tool.name == 'context_compile')
    for key in ('allow_personal_memory', 'allow_semantic_lane'):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({'query': 'synthetic', key: value}, schema)
        # The internal contract permits nullable legacy fields as defaults;
        # MCP rejects null at its schema boundary before calling this helper.
        if value is not None:
            with pytest.raises(ContractError):
                server._request('fixed-owner', {key: value})
