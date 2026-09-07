"""Human-configured teams cannot be rewritten by model-authored tasks."""
from copy import deepcopy
from unittest.mock import MagicMock
import pytest


def team():
    return {'enabled': True, 'members': [{'id': 'reviewer', 'name': 'Reviewer',
        'role': 'Review only', 'model': '', 'endpoint_id': '', 'write': False,
        'tools': [], 'files': ['report.md']}], 'max_parallel': 2, 'max_rounds': 8, 'timeout_s': 120}


@pytest.fixture
def store(tmp_path, monkeypatch):
    from src import chat_team
    monkeypatch.setattr(chat_team, 'DATA_DIR', tmp_path)
    return chat_team


def test_owner_scoping_and_compare_swap(store):
    assert store.load('session', 'alice')['revision'] == 0
    assert store.save('session', 'alice', team(), 0)['revision'] == 1
    assert store.load('session', 'bob')['team']['enabled'] is False
    with pytest.raises(ValueError, match='changed elsewhere'):
        store.save('session', 'alice', team(), 0)
    assert store.load('session', 'alice')['revision'] == 1


def test_corrupt_config_is_not_overwritten(store):
    path = store._path('session', 'alice')
    path.parent.mkdir()
    path.write_text('{broken', encoding='utf-8')
    with pytest.raises(ValueError):
        store.save('session', 'alice', team(), 0)
    assert path.read_text() == '{broken'


@pytest.mark.parametrize('patch', [{'max_parallel': True}, {'max_rounds': 100},
    {'timeout_s': 0}, {'members': []}, {'runner': 'codex'}, {'enabled': 'true'}])
def test_invalid_config_refused(store, patch):
    with pytest.raises(ValueError):
        store.validate({**team(), **patch})


def test_model_cannot_override_roster_or_escalate(store):
    args = {'tasks': [{'team_member': 'reviewer', 'name': 'imposter', 'task': 'Review',
        'model': 'other', 'endpoint_id': 'secret', 'runner': 'shell', 'resume': 'foreign',
        'agent': 'admin', 'files': ['other.txt']}], 'max_rounds': 20, 'timeout_s': 800, 'reviewer': True}
    result = store.bind_tasks(args, store.validate(team()), 'alice')
    worker = result['tasks'][0]
    assert worker['name'] == 'Reviewer' and worker['model'] == ''
    assert worker['files'] == ['report.md']
    assert worker['agent_def']['permission'] == ['deny write **']
    assert not any(k in worker for k in ('runner', 'resume', 'agent'))
    assert result['max_rounds'] == 8 and result['timeout_s'] == 120 and not result['reviewer']


def test_unknown_member_and_unavailable_route_refused(store, monkeypatch):
    args = {'tasks': [{'team_member': 'unknown'}], 'max_rounds': 8, 'timeout_s': 120}
    with pytest.raises(ValueError, match='configured team_member'):
        store.bind_tasks(deepcopy(args), team(), 'alice')
    from src import endpoint_resolver
    resolve = MagicMock(return_value=None)
    monkeypatch.setattr(endpoint_resolver, 'resolve_endpoint_by_id', resolve)
    cfg = team()
    cfg['members'][0].update(endpoint_id='private', model='model')
    args['tasks'][0]['team_member'] = 'reviewer'
    with pytest.raises(ValueError, match='No fallback'):
        store.bind_tasks(args, cfg, 'alice')
    resolve.assert_called_once_with('private', 'model', owner='alice', require_exact_model=True)


def test_team_route_owner_gate_and_conflict(store, monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from routes import session_routes as sr
    def verify(request, sid, manager):
        if sid != 'owned':
            raise HTTPException(404, 'Session not found')
    monkeypatch.setattr(sr, '_verify_session_owner', verify)
    monkeypatch.setattr(sr, 'effective_user', lambda request: 'alice')
    app = FastAPI()
    app.include_router(sr.setup_session_routes(MagicMock(), {}))
    with TestClient(app) as client:
        assert client.get('/api/session/foreign/team').status_code == 404
        body = {'revision': 0, 'team': team()}
        assert client.put('/api/session/owned/team', json=body, headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.put('/api/session/owned/team', json=body).status_code == 200
        assert client.put('/api/session/owned/team', json=body).status_code == 409
        assert client.get('/api/session/owned/team').json()['revision'] == 1


@pytest.mark.asyncio
async def test_delegation_enforces_team_routes_limits_and_queue(store, monkeypatch, tmp_path):
    import asyncio
    import json
    from types import SimpleNamespace
    from src import ai_interaction, tool_execution, endpoint_resolver
    from src.agent_tools import subagent_tools as st
    parent = SimpleNamespace(endpoint_url='http://coordinator/v1', model='coordinator', headers={'Authorization': 'parent'})
    monkeypatch.setattr(ai_interaction, 'get_session_manager', lambda: SimpleNamespace(get_session=lambda sid: parent))
    monkeypatch.setattr(tool_execution, 'get_active_workspace', lambda: str(tmp_path))
    monkeypatch.setattr(tool_execution, 'get_active_workspace_roots', lambda: [str(tmp_path)])
    monkeypatch.setattr(st, '_setting', lambda key, default=None: {'agent_subagent_worker_model': 'global-override', 'agent_subagent_max_parallel': 4}.get(key, default))
    resolve = MagicMock(return_value=('http://member/v1', 'chosen', {'Authorization': 'member'}))
    monkeypatch.setattr(endpoint_resolver, 'resolve_endpoint_by_id', resolve)
    # Profile resolution is orthogonal; keep actual permission derivation.
    monkeypatch.setattr(st, '_attach_resolution', lambda *args: None)
    monkeypatch.setattr(st, '_save_transcript', lambda *args, **kwargs: None)
    calls, events = [], []
    active = peak = 0
    async def worker(run, **kw):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append((run, kw))
        await asyncio.sleep(.04)
        run.text = 'done'
        active -= 1
    monkeypatch.setattr(st, '_run_subagent', worker)
    async def emit(event):
        events.append(event['subagent'])
    cfg = team()
    cfg['max_parallel'] = 1
    cfg['members'].append({**cfg['members'][0], 'id': 'explicit', 'name': 'Explicit', 'model': 'chosen', 'endpoint_id': 'private'})
    result = await st.DelegateAgentsTool().execute(json.dumps({'tasks': [
        {'team_member': 'reviewer', 'instruction': 'A'}, {'team_member': 'explicit', 'instruction': 'B'}],
        'parallel': True, 'reviewer': True, 'max_rounds': 40, 'timeout_s': 900}),
        {'session_id': 'parent', 'owner': 'alice', 'harness_options': {'chat_team': cfg}, 'progress_cb': emit})
    assert result['exit_code'] == 0 and len(calls) == 2 and peak == 1
    assert calls[0][1]['model'] == 'coordinator'  # not the global worker override
    assert calls[1][1]['model'] == 'chosen'
    assert calls[1][1]['headers'] == {'Authorization': 'member'}
    assert all(kw['max_rounds'] <= 8 and kw['timeout_s'] <= 120 for _, kw in calls)
    assert all(run.permissions is not None for run, _ in calls)
    assert any(event['event'] == 'queued' for event in events)
    assert all(call.kwargs == {'owner': 'alice', 'require_exact_model': True} for call in resolve.call_args_list)


def test_legacy_profile_route_does_not_leak_parent_credentials(monkeypatch):
    from src import endpoint_resolver
    from src.agent_tools import subagent_tools as st
    run = st.SubagentRun(0, {'name': 'Profile worker', 'instruction': 'Review', 'endpoint_id': 'other'})
    monkeypatch.setattr(endpoint_resolver, 'resolve_endpoint_by_id', lambda *args, **kwargs: ('http://other/v1', 'm', {}))
    assert st._route_for(run, 'http://parent/v1', 'alice', {'Authorization': 'parent-secret'}) == ('http://other/v1', {})
    monkeypatch.setattr(endpoint_resolver, 'resolve_endpoint_by_id', lambda *args, **kwargs: None)
    assert st._route_for(run, 'http://parent/v1', 'alice', {'Authorization': 'parent-secret'}) == ('http://parent/v1', {'Authorization': 'parent-secret'})
