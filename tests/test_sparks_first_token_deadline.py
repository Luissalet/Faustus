import asyncio
import json

from src.first_token_deadline import bounded


def test_wait_setting_is_supported_by_the_real_store():
    from src.settings import _validate_patch
    assert _validate_patch({"sparks_first_token_timeout_s": 5.0}) == {"sparks_first_token_timeout_s": 5.0}


def test_hung_first_read_closes_source():
    async def run():
        closed = []
        async def source():
            try:
                await asyncio.sleep(10)
                yield "unused"
            finally:
                closed.append(True)
        chunks = [chunk async for chunk in bounded(source(), 0.03)]
        assert closed == [True]
        assert len(chunks) == 1
        assert '"error_class": "first_token_timeout"' in chunks[0]
        assert '"retryable": false' in chunks[0]
    asyncio.run(run())


def test_heartbeats_do_not_reset_deadline():
    async def run():
        async def source():
            while True:
                await asyncio.sleep(0.01)
                yield 'data: {"type":"status","delta":""}\n\n'
        chunks = [chunk async for chunk in bounded(source(), 0.04)]
        assert 'first_token_timeout' in chunks[-1]
        assert len(chunks) < 8
    asyncio.run(run())


def test_thinking_starts_response_and_removes_initial_deadline():
    async def run():
        async def source():
            yield 'data: {"delta":"Checking","thinking":true}\n\n'
            await asyncio.sleep(0.06)
            yield 'data: {"delta":"Answer"}\n\n'
        chunks = [chunk async for chunk in bounded(source(), 0.02)]
        assert len(chunks) == 2
        assert "Answer" in chunks[-1]
    asyncio.run(run())


def test_error_and_empty_completion_are_preserved():
    async def run():
        async def source():
            yield 'event: error\ndata: {"error":"unavailable"}\n\n'
        chunks = [chunk async for chunk in bounded(source(), 0.05)]
        assert len(chunks) == 1
        assert "unavailable" in chunks[0]
    asyncio.run(run())


def test_timeout_is_visible_before_slow_cleanup_completes():
    async def run():
        released = asyncio.Event()
        async def source():
            try:
                await asyncio.sleep(10)
                yield "unused"
            finally:
                await released.wait()
        stream = bounded(source(), 0.02)
        event = await asyncio.wait_for(anext(stream), 0.2)
        assert 'first_token_timeout' in event
        released.set()
        await stream.aclose()
    asyncio.run(run())


def test_generator_context_is_owned_by_one_task():
    import contextvars
    async def run():
        value = contextvars.ContextVar("stream_owner", default="outside")
        async def source():
            token = value.set("inside")
            try:
                yield 'data: {"type":"status"}\n\n'
                yield 'data: {"delta":"answer"}\n\n'
            finally:
                value.reset(token)
        chunks = [chunk async for chunk in bounded(source(), 0.05)]
        assert len(chunks) == 2
        assert value.get() == "outside"
    asyncio.run(run())


def test_session_wait_timeout_preserves_the_active_turn_lock():
    import pytest
    from fastapi import HTTPException
    from routes.chat_routes import _session_admission_lock, _admission_locks
    async def run():
        async with _session_admission_lock("deadline-test"):
            with pytest.raises(HTTPException) as raised:
                async with _session_admission_lock("deadline-test", wait_s=0.02):
                    raise AssertionError("The second turn must not enter")
            assert raised.value.status_code == 504
            assert _admission_locks["deadline-test"].locked()
        assert "deadline-test" not in _admission_locks
    asyncio.run(run())
