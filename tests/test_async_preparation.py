"""Preparation yields to the loop without changing predicates or orphaning CPU work."""
import asyncio
import contextvars
import threading

import pytest

from src.async_preparation import run_preparation


async def _started(event):
    async with asyncio.timeout(5):
        while not event.is_set():
            await asyncio.sleep(.005)


@pytest.mark.asyncio
async def test_complete_arguments_result_and_contextvars_cross_the_worker():
    owner = contextvars.ContextVar('preparation_test_owner', default=None)
    token = owner.set('private-owner')
    original = 'área\r\n' * 100000
    result = object()
    caller_thread = threading.get_ident()

    def predicate(text, *, marker):
        assert text is original
        assert marker == 'unchanged'
        assert owner.get() == 'private-owner'
        assert threading.get_ident() != caller_thread
        owner.set('worker-only')
        return result

    try:
        assert await run_preparation(predicate, original, marker='unchanged') is result
        assert owner.get() == 'private-owner'
    finally:
        owner.reset(token)


@pytest.mark.asyncio
async def test_original_exception_propagates():
    error = ValueError('predicate failed')

    def predicate():
        raise error

    with pytest.raises(ValueError) as raised:
        await run_preparation(predicate)
    assert raised.value is error


@pytest.mark.asyncio
async def test_real_large_math_predicate_allows_a_concurrent_heartbeat():
    from src.agent_loop import _answer_only_math_request
    body = 'current-data-' * 300000
    running = threading.Event()
    finished = threading.Event()
    ticks = []

    def predicate():
        running.set()
        try:
            return _answer_only_math_request(body)
        finally:
            finished.set()

    async def heartbeat():
        while not finished.is_set():
            if running.is_set():
                ticks.append(asyncio.get_running_loop().time())
            await asyncio.sleep(.01)

    pulse = asyncio.create_task(heartbeat())
    try:
        assert await run_preparation(predicate) is False
    finally:
        await pulse
    assert ticks, 'the event loop must run while the real CPU predicate is active'


@pytest.mark.asyncio
async def test_authoritative_cancel_before_start_prevents_work():
    calls = []
    with pytest.raises(asyncio.CancelledError):
        await run_preparation(lambda: calls.append('started'), pending_cancel=lambda: 'stop')
    assert calls == []


@pytest.mark.asyncio
async def test_authoritative_cancel_after_work_prevents_following_stage():
    cancelled = threading.Event()
    following = []

    def predicate():
        cancelled.set()
        return 'complete'

    with pytest.raises(asyncio.CancelledError):
        await run_preparation(predicate, pending_cancel=lambda: 'stop' if cancelled.is_set() else None)
        following.append('model dispatch')
    assert following == []


@pytest.mark.asyncio
@pytest.mark.parametrize('late_failure', [False, True])
async def test_repeated_cancellation_joins_worker_and_never_advances(late_failure):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    following = []
    errors = []
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    def predicate():
        started.set()
        try:
            assert release.wait(5), 'owned fixture must be released'
            if late_failure:
                raise ValueError('failure during cancellation cleanup')
        finally:
            finished.set()

    async def owner():
        await run_preparation(predicate)
        following.append('model dispatch')

    task = asyncio.create_task(owner())
    try:
        await _started(started)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(.01)
        assert not task.done(), 'a cancelled owner must still own the running worker'
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert following == []
        await asyncio.sleep(0)
        assert errors == []
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)
        loop.set_exception_handler(old_handler)


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['math', 'ledger', 'trim'])
async def test_real_agent_cancel_during_preparation_cannot_dispatch(monkeypatch, stage):
    from src import agent_loop as al
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    dispatches = []
    main_thread = threading.get_ident()

    def predicate(*args):
        assert threading.get_ident() != main_thread
        started.set()
        try:
            assert release.wait(5)
            return False
        finally:
            finished.set()

    async def no_model(*args, **kwargs):
        dispatches.append('dispatch')
        yield 'data: [DONE]\n\n'

    if stage == 'math':
        monkeypatch.setattr(al, '_answer_only_math_request', predicate)
    elif stage == 'ledger':
        from src import agent_harness
        monkeypatch.setattr(agent_harness, 'TurnLedger', predicate)
    else:
        from src import llm_core
        monkeypatch.setattr(llm_core, 'stream_context_reserves', predicate)
    monkeypatch.setattr(al, 'get_setting', lambda key, default=None: default)
    monkeypatch.setattr(al, 'get_mcp_manager', lambda: None)
    monkeypatch.setattr(al, 'blocked_tools_for_owner', lambda owner: set())
    monkeypatch.setattr(al, 'stream_llm_with_fallback', no_model)
    monkeypatch.setattr(al, 'stream_llm', no_model)

    async def consume():
        async for _ in al.stream_agent_loop(
            'http://127.0.0.1:9/v1', 'fixture/cpu',
            [{'role': 'user', 'content': 'hello'}], max_rounds=1,
            relevant_tools={'bash'},
        ):
            pass

    task = asyncio.create_task(consume())
    try:
        await _started(started)
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert dispatches == []
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'hola', 'Edit @src/cart.py and tests/área_test.py.',
    'Explica esto sin herramientas ni comandos.',
    'Use only MCP tools to add a project objective.',
])
async def test_turn_builders_preserve_complete_results(text):
    from src import agent_loop, agent_harness, think_mode
    from src.tool_index import ToolIndex
    from src.tool_policy import build_effective_tool_policy

    expected_policy = build_effective_tool_policy(last_user_message=text)
    assert await run_preparation(build_effective_tool_policy, last_user_message=text) == expected_policy
    expected_think = think_mode.resolve_turn('auto', {}, text, model='fixture', agent=True)
    assert await run_preparation(think_mode.resolve_turn, 'auto', {}, text, model='fixture', agent=True) == expected_think
    expected_ledger = agent_harness.TurnLedger(None, text)
    actual_ledger = await run_preparation(agent_harness.TurnLedger, None, text)
    assert vars(actual_ledger) == vars(expected_ledger)
    assert actual_ledger.user_text is text
    assert await run_preparation(ToolIndex.keyword_tools_for_query, text, match_substrings=True) == ToolIndex.keyword_tools_for_query(text, match_substrings=True)
    assert await run_preparation(agent_loop._sampling_cap_scope_from_history, [], text) == agent_loop._sampling_cap_scope_from_history([], text)


@pytest.mark.asyncio
@pytest.mark.parametrize('message', ['hello', 'Fix src/cart.py', 'Use the calendar tool.'])
async def test_small_talk_policy_owns_only_its_private_payload(message):
    import copy
    from src.llm_core import _drop_tools_for_small_talk
    tools = [{'type': 'function', 'function': {'name': 'fixture'}}]
    original = {'messages': [{'role': 'user', 'content': message}], 'tools': tools,
                'tool_choice': 'auto', 'max_tokens': 777}
    expected = copy.deepcopy(original)
    wanted = _drop_tools_for_small_talk(expected, expected['messages'])
    actual = copy.deepcopy(original)
    assert await run_preparation(_drop_tools_for_small_talk, actual, actual['messages']) == wanted
    assert actual == expected
    assert original['tools'] == tools and original['tool_choice'] == 'auto'
