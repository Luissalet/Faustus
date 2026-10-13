import json

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from src.group_orchestrator import NativeGroupBridge
from tests.test_group_native_bridge import Sessions, turn, step


@pytest.mark.asyncio
async def test_native_provider_terminal_is_not_a_completed_group_step(monkeypatch):
    from src import chat_outbox
    monkeypatch.setattr(chat_outbox, 'get', lambda **kwargs: None)

    async def endpoint(request, **kwargs):
        async def stream():
            yield 'data: ' + json.dumps({'type': 'agent_terminal', 'data': {
                'failed': True, 'failure': {'message': 'Model request failed', 'status': 503}}}) + '\n\n'
            yield 'event: error\ndata: {"status":503,"text":"service unavailable"}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(stream(), headers={'X-Odysseus-Run-Id': 'failed-native'})

    async def progress(*args):
        pass
    bridge = NativeGroupBridge(Sessions(), endpoint, FastAPI())
    t = turn()
    result = await bridge.invoke(turn=t, step=step(), member=t['definition']['participants'][0],
                                 owner='owner', progress=progress)
    assert result['error'], result


@pytest.mark.asyncio
async def test_group_retains_uncertain_tool_status_and_reconciliation(monkeypatch):
    from src import chat_outbox
    monkeypatch.setattr(chat_outbox, 'get', lambda **kwargs: None)
    evidence = {'type': 'tool_output', 'tool': 'mcp__channels__send', 'output': 'receipt missing',
                'result_status': 'outcome_unknown', 'uncertainty': {'reason': 'lost receipt',
                 'reconcile_action': 'read history'}, 'attempt_id': 'attempt42',
                'effect_certainty': 'unknown', 'call_id': 'call42'}

    async def endpoint(request, **kwargs):
        async def stream():
            yield 'data: ' + json.dumps(evidence) + '\n\n'
            yield 'data: {"delta":"The result is uncertain."}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(stream(), headers={'X-Odysseus-Run-Id': 'uncertain-native'})

    async def progress(*args):
        pass
    bridge = NativeGroupBridge(Sessions(), endpoint, FastAPI())
    t = turn()
    result = await bridge.invoke(turn=t, step=step(), member=t['definition']['participants'][0],
                                 owner='owner', progress=progress)
    saved = result['detail']['tool_results'][0]
    for key in ('result_status', 'uncertainty', 'attempt_id', 'effect_certainty', 'call_id'):
        assert saved.get(key) == evidence[key], saved
