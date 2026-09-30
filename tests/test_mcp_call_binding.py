"""MCP executable and replay annotations remain one private call snapshot."""
import asyncio
import json
import pytest
from src import mcp_manager as mm
from src.mcp_manager import McpManager
from src.tool_result import normalize_tool_result, effect_state

@pytest.fixture
def fixture(monkeypatch):
    m = McpManager(); sid = 'synthetic'; first = object()
    m._sessions[sid] = first
    m._connections[sid] = {'name': 'fixture', 'status': 'connected', 'transport': 'stdio',
                           'owner_id': 'alice', 'secret_fixture': 'DO_NOT_OUTPUT'}
    m._tools[sid] = [{'name': 'mutate_record', 'input_schema': {'type': 'object'},
                      'annotations': {'readOnlyHint': False}}]
    entered, release = asyncio.Event(), asyncio.Event()
    calls, reconnects = [], []
    async def oauth(_):
        entered.set(); await release.wait(); return None
    async def execute(session, name, args):
        calls.append(session); return {'exit_code': 0, 'output': 'fixture'}
    async def reconnect(_):
        reconnects.append(True); m._sessions[sid] = object(); return True
    monkeypatch.setattr(m, '_oauth_ensure_valid', oauth)
    monkeypatch.setattr(m, '_do_call', execute)
    monkeypatch.setattr(m, '_reconnect_builtin', reconnect)
    monkeypatch.setattr(m, 'is_builtin', lambda _: True)
    monkeypatch.setattr(m, '_stdio_owner_alive', lambda _: True)
    return m, sid, first, entered, release, calls, reconnects

async def start(f):
    m, sid, _, entered, *_ = f
    task = asyncio.create_task(m.call_tool(f'mcp__{sid}__mutate_record', {}))
    await entered.wait(); return task

@pytest.mark.parametrize('change', ['session', 'session_removed', 'tool_removed', 'schema', 'hints', 'connection_removed',
                                    'config', 'owner', 'oauth_provider', 'owner_task'])
async def test_change_during_oauth_never_dispatches(fixture, change):
    m, sid, first, _, release, calls, reconnects = fixture
    task = await start(fixture)
    if change == 'session': m._sessions[sid] = object()
    elif change == 'session_removed': m._sessions.pop(sid)
    elif change == 'tool_removed': m._tools[sid].clear()
    elif change == 'schema': m._tools[sid][0]['input_schema']['required'] = ['new']
    elif change == 'hints': m._tools[sid][0]['annotations']['readOnlyHint'] = True
    elif change == 'connection_removed': m._connections.pop(sid)
    elif change == 'config': m._connections[sid]['transport'] = 'http'
    elif change == 'owner': m._connections[sid]['owner_id'] = 'bob'
    elif change == 'oauth_provider': m._oauth_providers[sid] = object()
    elif change == 'owner_task': m._owner_tasks[sid] = (object(), object())
    release.set(); result = await task
    assert result['error_code'] == 'MCP_CALL_BINDING_CHANGED'
    assert result['effect_not_dispatched'] and calls == [] and reconnects == []
    assert 'DO_NOT_OUTPUT' not in json.dumps(result)
    typed = normalize_tool_result(result)
    assert typed.status == 'denied' and effect_state(typed) == 'failed'

async def test_unchanged_scope_runs_captured_session_once(fixture):
    *_, release, calls, reconnects = fixture
    task = await start(fixture); release.set(); result = await task
    assert result['exit_code'] == 0 and calls == [fixture[2]] and reconnects == []

async def test_oauth_failure_preserved_without_dispatch(fixture, monkeypatch):
    m, sid, _, _, _, calls, reconnects = fixture
    async def denied(_): return {'error': 'reauthorization required', 'exit_code': 1, 'oauth_error': True}
    monkeypatch.setattr(m, '_oauth_ensure_valid', denied)
    result = await m.call_tool(f'mcp__{sid}__mutate_record', {})
    assert result['oauth_error'] and calls == [] and reconnects == []

async def test_cancelled_preflight_propagates_without_dispatch(fixture):
    task = await start(fixture); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert fixture[-2] == [] and fixture[-1] == []

async def test_session_scoped_browser_policy_rechecked_after_oauth(fixture, monkeypatch):
    from src.builtin_mcp import session_browser_server_id
    m, old, first, entered, release, calls, reconnects = fixture
    sid = session_browser_server_id('alice', 'task')
    m._sessions[sid] = m._sessions.pop(old)
    m._connections[sid] = m._connections.pop(old)
    m._tools[sid] = [{'name': 'browser_snapshot'}]
    denied = set(); monkeypatch.setattr(mm, 'builtin_browser_policy_disabled', lambda: denied)
    task = asyncio.create_task(m.call_tool(f'mcp__{sid}__browser_snapshot', {}))
    await entered.wait(); denied.add('*'); release.set()
    result = await task
    assert result['effect_not_dispatched'] and calls == [] and reconnects == []

@pytest.mark.parametrize('change', ['none', 'hints', 'schema', 'owner', 'policy'])
async def test_write_response_lost_is_never_replayed(fixture, monkeypatch, change):
    m, sid, first, _, release, calls, reconnects = fixture
    async def lost(session, name, args):
        calls.append(session)
        if change == 'hints': m._tools[sid][0]['annotations']['readOnlyHint'] = True
        elif change == 'schema': m._tools[sid][0]['input_schema']['required'] = ['new']
        elif change == 'owner': m._connections[sid]['owner_id'] = 'bob'
        elif change == 'policy': m._sessions.pop(sid)
        raise ConnectionError('DO_NOT_OUTPUT transport response')
    monkeypatch.setattr(m, '_do_call', lost)
    task = await start(fixture); release.set(); result = await task
    assert calls == [first] and result['status'] == 'outcome_unknown'
    assert 'DO_NOT_OUTPUT' not in json.dumps(result)
    assert normalize_tool_result(result).status == 'outcome_unknown'
    assert reconnects == []

@pytest.mark.parametrize('drift', [False, True])
async def test_readonly_recovery_requires_same_contract_scope(fixture, monkeypatch, drift):
    m, sid, first, _, release, calls, reconnects = fixture
    m._tools[sid][0]['annotations']['readOnlyHint'] = True
    async def lost_once(session, name, args):
        calls.append(session)
        if session is first: raise ConnectionError('fixture lost')
        return {'exit_code': 0}
    async def reconnect(_):
        reconnects.append(True); m._sessions[sid] = object()
        if drift: m._tools[sid][0]['annotations']['readOnlyHint'] = False
        return True
    monkeypatch.setattr(m, '_do_call', lost_once); monkeypatch.setattr(m, '_reconnect_builtin', reconnect)
    task = await start(fixture); release.set(); result = await task
    assert reconnects == [True]
    assert len(calls) == (1 if drift else 2)
    assert result.get('status') == ('outcome_unknown' if drift else None)

async def test_missing_legacy_metadata_cannot_gain_replay_hints(fixture):
    m, sid, _, _, release, calls, _ = fixture; m._tools[sid] = []
    task = await start(fixture)
    m._tools[sid] = [{'name': 'mutate_record', 'annotations': {'readOnlyHint': True}}]
    release.set(); result = await task
    assert result['effect_not_dispatched'] and calls == []


@pytest.mark.parametrize('provider_replaced', [False, True])
async def test_read_recovery_oauth_identity_is_conservative(fixture, monkeypatch, provider_replaced):
    m, sid, first, _, release, calls, reconnects = fixture
    provider = object(); m._oauth_providers[sid] = provider
    m._tools[sid][0]['annotations']['readOnlyHint'] = True
    async def execute(session, name, args):
        calls.append(session)
        if session is first: raise ConnectionError('fixture response lost')
        return {'exit_code': 0}
    async def reconnect(_):
        reconnects.append(True); m._sessions[sid] = object()
        if provider_replaced: m._oauth_providers[sid] = object()
        return True
    monkeypatch.setattr(m, '_do_call', execute)
    monkeypatch.setattr(m, '_reconnect_builtin', reconnect)
    task = await start(fixture); release.set(); result = await task
    assert len(calls) == (1 if provider_replaced else 2)
    assert result.get('status') == ('outcome_unknown' if provider_replaced else None)


async def test_legacy_missing_tool_metadata_stable_read_still_runs(fixture):
    m, sid, first, entered, release, calls, _ = fixture
    m._tools[sid] = []
    task = asyncio.create_task(m.call_tool(f'mcp__{sid}__list_records', {}))
    await entered.wait(); release.set(); result = await task
    assert result['exit_code'] == 0 and calls == [first]


async def test_read_recovery_cannot_adopt_new_connection_owner(fixture, monkeypatch):
    m, sid, first, _, release, calls, reconnects = fixture
    m._tools[sid][0]['annotations']['readOnlyHint'] = True
    async def execute(session, name, args):
        calls.append(session); raise ConnectionError('fixture response lost')
    async def reconnect(_):
        reconnects.append(True); m._sessions[sid] = object()
        m._connections[sid]['owner_id'] = 'bob'
        return True
    monkeypatch.setattr(m, '_do_call', execute)
    monkeypatch.setattr(m, '_reconnect_builtin', reconnect)
    task = await start(fixture); release.set(); result = await task
    assert result['status'] == 'outcome_unknown' and calls == [first]


async def test_cancelled_dispatched_call_never_reconnects(fixture, monkeypatch):
    m, sid, _, _, release, calls, reconnects = fixture
    async def execute(session, name, args):
        calls.append(session); raise asyncio.CancelledError()
    monkeypatch.setattr(m, '_do_call', execute)
    task = await start(fixture); release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert len(calls) == 1 and reconnects == []

async def test_scoped_browser_revocation_during_reconnect_prevents_retry(fixture, monkeypatch):
    from src.builtin_mcp import session_browser_server_id
    m, old, first, entered, release, calls, reconnects = fixture
    sid = session_browser_server_id('alice', 'task')
    m._sessions[sid] = m._sessions.pop(old)
    m._connections[sid] = m._connections.pop(old)
    m._tools[sid] = [{'name': 'browser_snapshot', 'annotations': {'readOnlyHint': True}}]
    denied = set(); monkeypatch.setattr(mm, 'builtin_browser_policy_disabled', lambda: denied)
    async def lost(session, name, args):
        calls.append(session); raise ConnectionError('fixture response lost')
    async def reconnect(_):
        reconnects.append(True); m._sessions[sid] = object(); denied.add('*'); return True
    monkeypatch.setattr(m, '_do_call', lost); monkeypatch.setattr(m, '_reconnect_builtin', reconnect)
    task = asyncio.create_task(m.call_tool(f'mcp__{sid}__browser_snapshot', {}))
    await entered.wait(); release.set(); result = await task
    assert result['status'] == 'outcome_unknown' and calls == [first] and reconnects == [True]


async def test_nested_caller_mutation_during_oauth_does_not_change_arguments(fixture, monkeypatch):
    m, sid, _, entered, release, calls, _ = fixture
    seen = []
    async def execute(session, name, args):
        seen.append(args); return {"exit_code": 0}
    monkeypatch.setattr(m, '_do_call', execute)
    arguments = {'nested': {'items': ['ENTRY']}}
    task = asyncio.create_task(m.call_tool(f'mcp__{sid}__mutate_record', arguments))
    await entered.wait(); arguments['nested']['items'].append('CALLER MUTATION')
    release.set(); await task
    assert seen == [{'nested': {'items': ['ENTRY']}}]
    assert arguments == {'nested': {'items': ['ENTRY', 'CALLER MUTATION']}}


async def test_arguments_captured_before_lazy_builtin_configuration_await(fixture, monkeypatch):
    m, old, first, _, release, calls, _ = fixture
    sid = mm.DEVTOOLS_MCP_SERVER_ID
    m._sessions[sid] = m._sessions.pop(old)
    m._connections[sid] = m._connections.pop(old)
    m._tools[sid] = m._tools.pop(old)
    checking, continue_check = asyncio.Event(), asyncio.Event()
    seen = []
    async def ensure():
        checking.set(); await continue_check.wait()
    async def oauth(_): return None
    async def execute(session, name, args):
        seen.append(args); return {'exit_code': 0}
    monkeypatch.setattr(m, 'ensure_builtin_devtools_current', ensure)
    monkeypatch.setattr(m, '_oauth_ensure_valid', oauth)
    monkeypatch.setattr(m, '_do_call', execute)
    arguments = {'nested': {'value': 'ENTRY'}}
    task = asyncio.create_task(m.call_tool(f'mcp__{sid}__mutate_record', arguments))
    await checking.wait(); arguments['nested']['value'] = 'LATE'
    continue_check.set(); await task
    assert seen == [{'nested': {'value': 'ENTRY'}}]


@pytest.mark.parametrize('read_only', [False, True])
async def test_sdk_mutation_cannot_change_captured_arguments_or_read_retry(fixture, monkeypatch, read_only):
    m, sid, first, _, release, calls, reconnects = fixture
    m._tools[sid][0]['annotations']['readOnlyHint'] = read_only
    seen = []
    async def execute(session, name, args):
        seen.append(args['nested']['items'][:]); calls.append(session)
        args['nested']['items'].append('SDK MUTATION')
        if session is first: raise ConnectionError('fixture response lost')
        return {'exit_code': 0}
    monkeypatch.setattr(m, '_do_call', execute)
    arguments = {'nested': {'items': ['ENTRY']}}
    task = asyncio.create_task(m.call_tool(f'mcp__{sid}__mutate_record', arguments))
    await fixture[3].wait(); release.set(); result = await task
    assert arguments == {'nested': {'items': ['ENTRY']}}
    assert seen == ([['ENTRY'], ['ENTRY']] if read_only else [['ENTRY']])
    assert len(reconnects) == (1 if read_only else 0)
    assert result.get('status') == (None if read_only else 'outcome_unknown')
