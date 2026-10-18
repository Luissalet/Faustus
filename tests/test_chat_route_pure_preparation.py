"""The real route yields pure work; decisions and mutations keep their owner."""
import asyncio
import contextvars
import threading
from types import SimpleNamespace

import pytest

from src import freshness


class _FollowingStage(BaseException):
    """End the route fixture before context construction or any dispatch."""


@pytest.fixture
def route_case(monkeypatch, tmp_path):
    from routes import chat_routes as routes
    from src import agent_loop
    from tests.test_lote20_chat_routes_budget_exhausted import (
        _chat_stream_endpoint, _RouteRequest,
    )

    original = 'área\r\n' * 100000 + 'complete-tail-ñ'

    def configure(stage, predicate):
        endpoint = _chat_stream_endpoint(monkeypatch, [])
        request = _RouteRequest()
        request._form['message'] = original
        monkeypatch.setattr(routes, 'coerce_message_and_session',
                            lambda *a, **k: (original, 'session-budget-exhausted'))
        monkeypatch.setattr(routes, '_project_workspace', lambda *a: '')
        monkeypatch.setattr(routes, '_project_work_roots', lambda *a: [])
        monkeypatch.setattr(routes, '_project_harness_options', lambda *a: {})

        def following(*args, **kwargs):
            raise _FollowingStage()

        if stage in ('freshness', 'freshness_fallback'):
            if stage == 'freshness':
                monkeypatch.setattr(freshness, 'freshness_assessment', predicate)
            else:
                async def unavailable(*args, **kwargs):
                    raise ValueError('decision unavailable')
                monkeypatch.setattr(freshness, 'decide_freshness', unavailable)
                monkeypatch.setattr(freshness, 'looks_time_sensitive', predicate)
            monkeypatch.setattr(routes, '_classify_tool_intent', following)
        else:
            request._form.update(allow_web_search='false',
                                 mode='chat' if stage == 'promotion' else 'agent')
            monkeypatch.setattr(routes, '_resolve_request_workspace',
                                lambda *a: (str(tmp_path), ''))
            monkeypatch.setattr(routes, '_classify_tool_intent', lambda *a: None)
            monkeypatch.setattr(routes, '_is_contextual_web_followup', lambda *a: False)
            monkeypatch.setattr(routes, '_is_contextual_browser_followup', lambda *a: False)
            monkeypatch.setattr(routes, '_looks_like_workspace_coding_request', predicate)
            monkeypatch.setattr(agent_loop, '_looks_like_workspace_coding_request', predicate)
            if stage == 'promotion':
                monkeypatch.setattr(routes, '_enforce_chat_privileges', following)
            else:
                monkeypatch.setattr(routes, 'build_effective_tool_policy', following)
        return endpoint, request

    return configure, original


@pytest.mark.parametrize('stage', ['freshness', 'freshness_fallback', 'promotion', 'memory'])
@pytest.mark.parametrize('verdict', [False, True])
@pytest.mark.asyncio
async def test_real_route_passes_complete_input_to_worker(route_case, stage, verdict):
    configure, original = route_case
    loop_thread = threading.get_ident()
    owner = contextvars.ContextVar('route_preparation_owner', default=None)
    token = owner.set('owner890')
    seen = []

    def predicate(text):
        seen.append((text is original, threading.get_ident(), owner.get()))
        if stage == 'freshness':
            return dict(time_sensitive=verdict, confident=True, reasons=[], why='long_or_code')
        return verdict

    try:
        endpoint, request = configure(stage, predicate)
        with pytest.raises(_FollowingStage):
            await endpoint(request)
        assert len(seen) == 1 and seen[0][0] is True and seen[0][2] == 'owner890'
        assert seen[0][1] != loop_thread, 'pure route work must leave the loop thread'
        assert owner.get() == 'owner890'
    finally:
        owner.reset(token)


@pytest.mark.parametrize('stage,gate', [
    ('promotion', 'no_workspace'), ('memory', 'no_workspace'), ('memory', 'setting_off'),
])
@pytest.mark.asyncio
async def test_workspace_short_circuit_still_skips_predicate(route_case, monkeypatch, stage, gate):
    from routes import chat_routes as routes
    from src import settings
    configure, _ = route_case
    calls = []
    endpoint, request = configure(stage, lambda text: calls.append(text))
    if gate == 'no_workspace':
        monkeypatch.setattr(routes, '_resolve_request_workspace', lambda *a: (None, ''))
        monkeypatch.setattr(routes, '_resolve_workspace_from_message_path', lambda *a: (None, ''))
    else:
        monkeypatch.setattr(settings, 'get_setting',
                            lambda key, default=None: False if key == 'agent_workspace_no_memory' else default)
    with pytest.raises(_FollowingStage):
        await endpoint(request)
    assert calls == []


@pytest.mark.parametrize('stage', ['freshness', 'freshness_fallback', 'promotion', 'memory'])
@pytest.mark.parametrize('late_failure', [False, True])
@pytest.mark.asyncio
async def test_route_repeated_cancel_joins_work_without_advancing(route_case, monkeypatch, stage, late_failure):
    configure, original = route_case
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    beats = []
    decisions = []
    from src import typed_decision

    async def decide(*args, **kwargs):
        decisions.append('model dispatch')
        raise _FollowingStage()

    monkeypatch.setattr(typed_decision, 'enabled', lambda: True)
    monkeypatch.setattr(typed_decision, '_setting', lambda key: True)
    monkeypatch.setattr(typed_decision, 'decide', decide)

    def predicate(text):
        assert text is original
        assert threading.get_ident() != loop_thread
        started.set()
        try:
            assert release.wait(5), 'fixture must release its owned worker'
            if late_failure:
                raise ValueError('failure after cancellation')
            return dict(time_sensitive=False, confident=False, reasons=[], why='fixture') if stage == 'freshness' else True
        finally:
            finished.set()

    endpoint, request = configure(stage, predicate)
    task = asyncio.create_task(endpoint(request))
    try:
        async with asyncio.timeout(5):
            while not started.is_set() and not task.done():
                await asyncio.sleep(0)
        assert started.is_set()
        for _ in range(3):
            beats.append('loop is responsive while worker waits')
            task.cancel()
            await asyncio.sleep(0)
        assert beats and not task.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert decisions == []
        # Reaching the next route stage would raise _FollowingStage instead.
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('confident', [True, False])
@pytest.mark.asyncio
async def test_freshness_keeps_typed_decision_on_loop(monkeypatch, confident):
    from src import typed_decision
    text = 'unchanged input\r\nñ'
    loop_thread = threading.get_ident()
    threads = []
    assessment = dict(time_sensitive=True, confident=confident, reasons=['fixture'], why='fixture')

    def assess(value):
        assert value is text
        threads.append(threading.get_ident())
        return assessment

    decision = SimpleNamespace(value='no', best='no', confidence=.99, mass=.99,
                               method='logprobs', reason='', ms=1)
    calls = []

    def enabled():
        assert threading.get_ident() == loop_thread
        return True

    async def decide(value, fields, **kwargs):
        assert threading.get_ident() == loop_thread
        assert value is text
        calls.append((fields[0].name, kwargs))
        return {'needs_web': decision}

    monkeypatch.setattr(freshness, 'freshness_assessment', assess)
    monkeypatch.setattr(typed_decision, 'enabled', enabled)
    monkeypatch.setattr(typed_decision, '_setting', lambda key: True)
    monkeypatch.setattr(typed_decision, 'decide', decide)
    out = await freshness.decide_freshness(text, owner='owner890')
    assert len(threads) == 1 and threads[0] != loop_thread
    assert out == dict(time_sensitive=confident, source='rule' if confident else 'typed_decision',
                       reasons=['fixture'], why='fixture', decision=None if confident else vars(decision))
    assert calls == ([] if confident else [('needs_web', {'owner': 'owner890', 'caller': 'freshness'})])


@pytest.mark.asyncio
async def test_freshness_error_fallback_runs_once_in_worker(monkeypatch):
    loop_thread = threading.get_ident()
    calls = []
    text = 'full fallback input'

    def assess(value):
        assert value is text and threading.get_ident() != loop_thread
        calls.append('assessment')
        raise ValueError('rule unavailable')

    def fallback(value):
        assert value is text and threading.get_ident() != loop_thread
        calls.append('fallback')
        return True

    monkeypatch.setattr(freshness, 'freshness_assessment', assess)
    monkeypatch.setattr(freshness, 'looks_time_sensitive', fallback)
    assert await freshness.decide_freshness(text) == dict(
        time_sensitive=True, source='rule', reasons=[], why='error', decision=None)
    assert calls == ['assessment', 'fallback']


@pytest.mark.asyncio
async def test_freshness_fallback_exception_keeps_identity(monkeypatch):
    error = ValueError('fallback error')

    def assess(text):
        raise RuntimeError('assessment error')

    def fallback(text):
        raise error

    monkeypatch.setattr(freshness, 'freshness_assessment', assess)
    monkeypatch.setattr(freshness, 'looks_time_sensitive', fallback)
    with pytest.raises(ValueError) as raised:
        await freshness.decide_freshness('unchanged')
    assert raised.value is error
