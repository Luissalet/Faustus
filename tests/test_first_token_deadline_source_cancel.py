"""Source termination and real task cancellation through the bounded stream."""
import asyncio
import contextvars

import pytest

from src.first_token_deadline import bounded


DELTA = 'data: {"delta":"first"}\n\n'
VALUE = contextvars.ContextVar('source_cancel_owner', default='outside')


class Source:
    """Private async source records ownership, close calls and context reset."""
    def __init__(self, body):
        self.body = body
        self.owner = None
        self.closed = 0
        self.finalized = 0
        self.cancelling_at_exit = None
        self.iterator = self.output()

    async def output(self):
        self.owner = asyncio.current_task()
        token = VALUE.set('inside')
        inner = self.body()
        try:
            async for chunk in inner:
                yield chunk
        finally:
            self.finalized += 1
            self.cancelling_at_exit = asyncio.current_task().cancelling()
            assert asyncio.current_task() is self.owner
            assert VALUE.get() == 'inside'
            await inner.aclose()
            VALUE.reset(token)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await anext(self.iterator)

    async def aclose(self):
        self.closed += 1
        assert asyncio.current_task() is self.owner
        await self.iterator.aclose()


async def no_workers(source):
    await asyncio.sleep(0)
    assert source.closed == source.finalized == 1
    assert VALUE.get() == 'outside'
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def expect_source_cancel(stream, source, expected):
    got = []
    async def consume():
        async for chunk in stream:
            got.append(chunk)
    consumer = asyncio.create_task(consume())
    try:
        done, _ = await asyncio.wait({consumer}, timeout=0.15)
        assert done, 'Source terminated, but bounded never delivered its cancellation'
        assert consumer.cancelling() == 0, 'Fixture must not manufacture Task.cancel'
        with pytest.raises(asyncio.CancelledError):
            consumer.result()
        assert got == expected
        assert source.cancelling_at_exit == 0
    finally:
        if not consumer.done():
            consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await stream.aclose()
        await no_workers(source)


@pytest.mark.parametrize('content', [False, True])
@pytest.mark.parametrize('phase', ['model', 'queue'])
def test_source_cancel_reaches_consumer_without_external_cancel(content, phase):
    async def run():
        async def body():
            if content:
                yield DELTA
            raise asyncio.CancelledError('upstream source stopped')
        source = Source(body)
        await expect_source_cancel(bounded(source, 5, phase=phase), source, [DELTA] if content else [])
    asyncio.run(run())


@pytest.mark.parametrize('content', [False, True])
def test_normal_end_and_runtime_error_keep_their_original_outcome(content):
    async def run():
        for failure in (False, True):
            async def body():
                if content:
                    yield DELTA
                if failure:
                    raise RuntimeError('ordinary source failure')
            source = Source(body)
            got = []
            async def consume():
                async for chunk in bounded(source, 5):
                    got.append(chunk)
            if failure:
                with pytest.raises(RuntimeError, match='ordinary source failure'):
                    await consume()
            else:
                await consume()
            assert got == ([DELTA] if content else [])
            await no_workers(source)
    asyncio.run(run())


@pytest.mark.parametrize('stage', ['before', 'after', 'backpressure'])
def test_real_consumer_cancel_closes_once_in_owner_task(stage):
    async def run():
        entered = asyncio.Event()
        emitted = []
        async def body():
            if stage == 'before':
                entered.set()
                await asyncio.Event().wait()
            for number in range(4):
                emitted.append(number)
                if stage == 'backpressure' and number == 2:
                    entered.set()
                yield DELTA if number == 0 else str(number)
                if stage == 'after':
                    entered.set()
                    await asyncio.Event().wait()
        source = Source(body)
        stream = bounded(source, 5)
        async def consume():
            try:
                async for _ in stream:
                    if stage == 'backpressure':
                        await asyncio.Event().wait()
            finally:
                await stream.aclose()
        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0)
        if stage == 'backpressure':
            assert emitted == [0, 1, 2], 'Queue must retain its one-item backpressure'
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, 1)
        assert source.cancelling_at_exit > 0
        await no_workers(source)
    asyncio.run(run())


def test_early_close_releases_backpressured_source_without_cancelling_caller():
    async def run():
        blocked = asyncio.Event()
        emitted = []
        async def body():
            for number in range(5):
                emitted.append(number)
                if number == 2:
                    blocked.set()
                yield DELTA if number == 0 else str(number)
        source = Source(body)
        stream = bounded(source, 5)
        assert await anext(stream) == DELTA
        await asyncio.wait_for(blocked.wait(), 1)
        await asyncio.sleep(0)
        assert emitted == [0, 1, 2]
        await asyncio.wait_for(stream.aclose(), 1)
        assert asyncio.current_task().cancelling() == 0
        assert source.cancelling_at_exit > 0
        await no_workers(source)
    asyncio.run(run())


@pytest.mark.parametrize('outer', ['bounded', 'scoped_stream'])
def test_source_cancel_survives_both_wrapper_orders(outer):
    from src.first_token_budget import effective, scoped_stream
    async def run():
        async def body():
            yield DELTA
            raise asyncio.CancelledError('source inside composed wrappers')
        source = Source(body)
        if outer == 'bounded':
            stream = bounded(scoped_stream(source, 120), 5)
        else:
            stream = scoped_stream(bounded(source, 5), 120)
        await expect_source_cancel(stream, source, [DELTA])
        assert effective(None) is None
    asyncio.run(run())
