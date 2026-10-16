"""Production workers use asyncio.run in a thread beside the web event loop."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

from src import llm_core


def test_repeated_worker_loops_can_contend_for_the_same_slot(monkeypatch):
    lock = llm_core._CrossLoopModelLock()
    monkeypatch.setattr(llm_core, '_LOCAL_MODEL_LOCK', lock)

    async def scenario():
        await lock.acquire()
        waiter = asyncio.create_task(llm_core._acquire_local_model_lock('chat'))
        await asyncio.sleep(0.05)
        lock.release()
        await asyncio.wait_for(waiter, timeout=1)
        lock.release()

    asyncio.run(scenario())
    asyncio.run(scenario())


def test_slot_remains_exclusive_across_worker_and_chat_loops(monkeypatch):
    lock = llm_core._CrossLoopModelLock()
    monkeypatch.setattr(llm_core, '_LOCAL_MODEL_LOCK', lock)
    holder_ready = threading.Event()
    release_holder = threading.Event()
    waiter_started = threading.Event()
    entered = threading.Event()

    async def hold():
        await llm_core._acquire_local_model_lock('worker')
        holder_ready.set()
        try:
            while not release_holder.is_set():
                await asyncio.sleep(0.005)
        finally:
            lock.release()

    async def wait():
        waiter_started.set()
        await llm_core._acquire_local_model_lock('chat')
        entered.set()
        lock.release()

    with ThreadPoolExecutor(2) as pool:
        holder = pool.submit(asyncio.run, hold())
        assert holder_ready.wait(2)
        waiter = pool.submit(asyncio.run, wait())
        try:
            assert waiter_started.wait(2)
            assert not entered.wait(0.1)
        finally:
            release_holder.set()
        holder.result(timeout=3)
        waiter.result(timeout=3)
    assert entered.is_set() and not lock.locked()
    # A third event loop must still be able to contend and acquire.
    asyncio.run(wait())


def test_cancelled_waiter_cannot_take_slot_later():
    lock = llm_core._CrossLoopModelLock()

    async def scenario():
        await lock.acquire()
        waiter = asyncio.create_task(lock.acquire())
        await asyncio.sleep(0.05)
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        lock.release()
        await asyncio.sleep(0.05)
        assert not lock.locked()
        await asyncio.wait_for(lock.acquire(), timeout=1)
        lock.release()

    asyncio.run(scenario())


def test_resident_remote_sparks_do_not_wait_for_another_model_slot(monkeypatch):
    lock = llm_core._CrossLoopModelLock()
    monkeypatch.setattr(llm_core, '_LOCAL_MODEL_LOCK', lock)
    monkeypatch.setattr(llm_core, 'is_local_endpoint', lambda url: True)
    monkeypatch.setattr(llm_core, 'is_sparks_url', lambda url: True)

    async def scenario():
        await lock.acquire()
        try:
            async def call():
                async with llm_core._local_model_slot_gate('http://192.168.0.236:8002/v1', 'glm'):
                    return 'independent'
            assert await asyncio.wait_for(call(), timeout=0.5) == 'independent'
            assert lock.locked()  # It neither released nor replaced Spark 4's slot.
        finally:
            lock.release()

    asyncio.run(scenario())
