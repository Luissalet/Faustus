"""Studio controls reach native member execution without changing group identity."""
import copy
import json

import pytest
from fastapi import FastAPI
from starlette.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
from routes.group_routes import setup_group_routes

from src.group_orchestrator import GroupCoordinator, GroupStore, NativeGroupBridge, composer_options
from test_group_native_bridge import Sessions, turn, step
from test_group_orchestrator import Invoker, Transcript, definition, idle


async def test_composer_fields_reach_native_chat_and_member_reasoning_wins(monkeypatch):
    from src import chat_outbox
    monkeypatch.setattr(chat_outbox, 'get', lambda **kwargs: None)
    seen = []

    async def endpoint(request, initial_response_timeout_s=None):
        seen.append((await request.json(), initial_response_timeout_s))

        async def stream():
            yield 'data: {"delta":"answer"}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(stream())

    t = turn()
    options = {'attachments': json.dumps(['upload-id']), 'gen_overrides': json.dumps({'temperature': .4}),
               'allow_bash': False, 'allow_web_search': False, 'use_rag': True, 'use_research': True,
               'think_mode': 'deep', 'thinking_enabled': True, 'reasoning_effort': 'low',
               'initial_response_timeout_s': 420, 'no_memory': True, 'no_skills': True,
               'input_token_budget': 6000, 'doc_context': json.dumps({'include': False}),
               'behavior_mode': 'default', 'chat_mode': 'normal'}
    t['definition']['executionOptions'] = copy.deepcopy(options)
    member = t['definition']['participants'][0]
    member.update(thinkingEnabled=False, reasoningEffort='high')
    bridge = NativeGroupBridge(Sessions(), endpoint, FastAPI())

    async def progress(*args):
        pass

    await bridge.invoke(turn=t, step=step(), member=member, owner='owner', progress=progress)
    body, timeout = seen[0]
    assert timeout == 420
    assert {key: body[key] for key in options if key not in ('thinking_enabled', 'reasoning_effort')} == {
        key: value for key, value in options.items() if key not in ('thinking_enabled', 'reasoning_effort')}
    assert body['thinking_enabled'] is False and body['reasoning_effort'] == 'high'
    assert body['session'] == 'a' and body['client_message_id'] == 'step'
    assert body['model'] == member['route']['model'] and body['mode'] == 'agent'
    assert t['definition']['executionOptions'] == options


@pytest.mark.parametrize('field', ['session', 'model', 'selected_endpoint_id', 'client_message_id', 'mode', 'workspace'])
def test_composer_cannot_replace_member_routing(field):
    with pytest.raises(ValueError):
        composer_options({field: 'another'})


def test_group_history_preserves_uploaded_attachments_without_duplicate_user_message():
    sessions = Sessions()
    bridge = NativeGroupBridge(sessions, None, FastAPI())
    t = turn()
    t['definition']['executionOptions'] = {'attachments': json.dumps(['upload-id'])}
    bridge.user(t, 'owner')
    bridge.user(t, 'owner')
    messages = sessions.get_session('parent').history
    assert len(messages) == 1 and messages[0].metadata['attachments'] == ['upload-id']


async def test_settings_are_snapshotted_for_queued_work_not_changed_mid_turn(tmp_path):
    invoker = Invoker()
    driver = GroupCoordinator(invoker, Transcript(), store=GroupStore(tmp_path / 'groups.db'))
    state = driver.adopt(definition(), 'owner')
    state = driver.update('parent', 'owner', {'executionOptions': {'allow_bash': False, 'gen_overrides': '{"temperature":0.4}'}}, state['revision'])
    first = await driver.submit('parent', 'owner', 'first', 'first-id')
    await invoker.started.wait()
    state = driver.store.group('parent', 'owner')
    state = driver.update('parent', 'owner', {'executionOptions': {'allow_bash': True}}, state['revision'])
    second = await driver.submit('parent', 'owner', 'second', 'second-id')
    queued = {item['id']: item for item in driver.store.turns('parent')}
    assert queued[first['turn_id']]['definition']['executionOptions']['allow_bash'] is False
    assert queued[second['turn_id']]['definition']['executionOptions'] == {'allow_bash': True}
    invoker.release.set()
    await idle(driver)


async def test_http_send_includes_its_options_while_another_turn_is_running(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTH_ENABLED', 'false')
    invoker = Invoker()
    driver = GroupCoordinator(invoker, Transcript(), store=GroupStore(tmp_path / 'groups.db'))
    driver.adopt(definition(), None)
    app = FastAPI()
    app.include_router(setup_group_routes(driver))
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://127.0.0.1') as client:
        first = await client.post('/api/groups/parent/messages', json={'message': 'first', 'request_id': 'first',
            'executionOptions': {'allow_bash': False}, 'executionMode': 'chat'})
        assert first.status_code == 200
        await invoker.started.wait()
        # Streaming progress has advanced the revision; this send needs no
        # client-side PATCH or stale revision and never cancels the first run.
        second = await client.post('/api/groups/parent/messages', json={'message': 'second', 'request_id': 'second',
            'executionOptions': {'allow_bash': True}, 'executionMode': 'agent'})
        assert second.status_code == 200 and not invoker.stops
        turns = driver.store.turns('parent')
        assert [turn['definition']['executionOptions']['allow_bash'] for turn in turns] == [False, True]
        assert [turn['definition']['executionMode'] for turn in turns] == ['chat', 'agent']
        bad = await client.post('/api/groups/parent/messages', json={'message': 'bad', 'executionOptions': {'session': 'other'}})
        assert bad.status_code == 400 and len(driver.store.turns('parent')) == 2
    invoker.release.set()
    await idle(driver)
