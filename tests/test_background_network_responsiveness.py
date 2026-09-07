import asyncio
import threading
from types import SimpleNamespace

import pytest


async def assert_responsive(coroutine, entered, release):
    task = asyncio.create_task(coroutine)
    try:
        for _ in range(200):
            if entered.is_set():
                break
            if task.done():
                await task  # expose the underlying error instead of a wait assertion
            await asyncio.sleep(.005)
        assert entered.is_set(), 'network operation did not start'
        assert not task.done(), 'the network wait blocked the event loop'
        release.set()
        return await asyncio.wait_for(task, 2)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked_stage', ['configuration', 'smtp'])
async def test_task_email_wait_never_blocks_chat_loop(monkeypatch, blocked_stage):
    from src.task_scheduler import TaskScheduler
    entered, release = threading.Event(), threading.Event()
    calls = []
    main_thread = threading.get_ident()
    def block():
        entered.set()
        release.wait(2)
    def config(**kwargs):
        assert threading.get_ident() != main_thread
        assert kwargs == {'account_id': 'mail-alice', 'owner': 'alice'}
        if blocked_stage == 'configuration':
            block()
        return {'from_address': 'alice@example.invalid', 'smtp_user': 'alice'}
    def smtp(cfg, sender, recipients, body, **kwargs):
        assert threading.get_ident() != main_thread
        calls.append((sender, recipients, body, kwargs))
        if blocked_stage == 'smtp':
            block()
    monkeypatch.setattr('routes.email_routes._resolve_send_config', config)
    monkeypatch.setattr('routes.email_helpers._send_smtp_message', smtp)
    task = SimpleNamespace(owner='alice', id='task-test', name='Report')
    scheduler = TaskScheduler.__new__(TaskScheduler)
    await assert_responsive(scheduler._deliver_via_email(
        'email:self|account=mail-alice', task, 'Result'), entered, release)
    assert len(calls) == 1
    assert calls[0][0:2] == ('alice@example.invalid', ['alice@example.invalid'])
    assert calls[0][3] == {'timeout': 30}
    assert 'Result' in calls[0][2]


@pytest.mark.asyncio
async def test_research_fallback_wait_never_blocks_chat_loop(monkeypatch):
    from src.research_handler import ResearchHandler
    service = ResearchHandler.__new__(ResearchHandler)
    service._legacy_engine = None
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    def search(query, error):
        assert threading.get_ident() != main_thread
        assert (query, error) == ('topic', 'offline')
        entered.set()
        release.wait(2)
        return 'fallback report'
    monkeypatch.setattr(service, '_handle_research_failure', search)
    assert await assert_responsive(service._fallback_research(
        'topic', 'unused', 'model', 10, 'offline'), entered, release) == 'fallback report'


@pytest.mark.asyncio
async def test_task_email_error_still_propagates(monkeypatch):
    from src.task_scheduler import TaskScheduler
    def fail(**kwargs):
        raise ValueError('account unavailable')
    monkeypatch.setattr('routes.email_routes._resolve_send_config', fail)
    with pytest.raises(ValueError, match='account unavailable'):
        await TaskScheduler.__new__(TaskScheduler)._deliver_via_email(
            'email:self', SimpleNamespace(owner='alice', id='test', name='Report'), 'Result')
