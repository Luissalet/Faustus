"""Per-turn no_skills must suppress the shared preface catalog before the agent loop."""
from types import SimpleNamespace

import pytest

from routes import chat_helpers
from src.chat_processor import ChatProcessor
from tests.test_behavior_mode_injection import _harness
from tests.test_lote20_chat_routes_budget_exhausted import _chat_stream_endpoint, _RouteRequest


@pytest.mark.asyncio
@pytest.mark.parametrize('skip', [None, False, True])
@pytest.mark.parametrize('preference', [False, True])
async def test_shared_preface_respects_request_and_owner_skill_controls(monkeypatch, skip, preference):
    sess, request, _ = _harness(monkeypatch)
    monkeypatch.setattr(chat_helpers, 'load_prefs_for_user',
                        lambda owner: {'skills_enabled': preference, 'memory_enabled': True})
    monkeypatch.setattr(chat_helpers, 'project_system_prompt', lambda *a: 'OWN PROJECT INSTRUCTIONS')
    index_reads = []

    def index_for(owner):
        index_reads.append(owner)
        return [{'name': 'own-skill', 'description': 'OWN SKILL INDEX', 'category': 'own'}]

    processor = ChatProcessor(
        memory_manager=SimpleNamespace(load=lambda **k: []),
        personal_docs_manager=SimpleNamespace(rag_manager=None),
        skills_manager=SimpleNamespace(index_for=index_for),
    )
    options = {} if skip is None else {'no_skills': skip}
    ctx = await chat_helpers.build_chat_context(
        sess, request, SimpleNamespace(), processor,
        message='Implement the synthetic project task', session_id='own-sid',
        agent_mode=True, use_rag=False, no_memory=True, **options,
    )
    expected = preference and not skip
    assert bool(index_reads) is expected
    assert ('OWN SKILL INDEX' in str(ctx.messages)) is expected
    assert ctx.messages[0]['content'] == 'OWN PROJECT INSTRUCTIONS'
    assert ctx.messages[-1] == {'role': 'user', 'content': 'Implement the synthetic project task'}
    assert sess.messages == [ctx.messages[-1]]


class _ContextCaptured(BaseException):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize('as_json', [False, True])
@pytest.mark.parametrize('skip', [False, True])
async def test_stream_route_carries_no_skills_to_shared_context(monkeypatch, as_json, skip):
    from routes import chat_routes
    endpoint = _chat_stream_endpoint(monkeypatch, [])
    seen = []

    async def capture(*args, **kwargs):
        seen.append(kwargs)
        raise _ContextCaptured()

    monkeypatch.setattr(chat_routes, 'build_chat_context', capture)
    request = _RouteRequest()
    if as_json:
        request.headers['content-type'] = 'application/json'
        async def body():
            return {'session': 'session-budget-exhausted', 'message': 'hello', 'mode': 'agent', 'no_skills': skip}
        request.json = body
    else:
        request._form['no_skills'] = str(skip).lower()
    with pytest.raises(_ContextCaptured):
        await endpoint(request)
    assert len(seen) == 1
    assert seen[0].get('no_skills', False) is skip
