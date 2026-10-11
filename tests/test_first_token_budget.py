import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from starlette.testclient import TestClient

from src.first_token_budget import _OVERRIDE, effective, parse, request_budget, scoped_stream
from src.first_token_deadline import bounded


@pytest.mark.parametrize("value", [True, False, {}, [], "nan", "inf", float("nan"), "4.9", "901", "bad"])
def test_bad_budget_is_rejected(value):
    with pytest.raises(ValueError):
        parse(value)


def test_absent_keeps_default_and_valid_override():
    assert parse(None) is None
    assert parse("") is None
    assert parse("900") == 900
    assert effective(30) == 30
    token = _OVERRIDE.set(120)
    try:
        assert effective(30) == 120
        assert effective(None) == 120
    finally:
        _OVERRIDE.reset(token)
    assert effective(None) is None


@pytest.mark.asyncio
async def test_parallel_request_context_and_detached_task_remain_separate():
    gate = asyncio.Event()
    async def call(seconds):
        token = _OVERRIDE.set(seconds)
        async def child():
            await gate.wait()
            return effective(30)
        try:
            child_task = asyncio.create_task(child())
        finally:
            _OVERRIDE.reset(token)
        assert effective(30) == 30
        return child_task
    one, two = await asyncio.gather(call(120), call(300))
    gate.set()
    assert await asyncio.gather(one, two) == [120, 300]
    assert effective(30) == 30


def test_native_endpoint_accepts_json_and_form_without_mutating_default():
    app = FastAPI()
    invocations = []
    @app.post("/turn")
    @request_budget
    async def turn(request: Request):
        invocations.append(effective(30))
        async def output():
            yield f'data: {json.dumps({"budget": effective(30)})}\n\n'
        return StreamingResponse(output(), media_type="text/event-stream")
    with TestClient(app) as client:
        response = client.post("/turn", json={"initial_response_timeout_s": 300})
        assert response.status_code == 200
        assert response.headers["X-Faustus-Initial-Response-Timeout-S"] == "300.0"
        assert '"budget": 300.0' in response.text
        assert client.post("/turn", data={"initial_response_timeout_s": "120"}).status_code == 200
        assert client.post("/turn", json={}).status_code == 200
        for invalid in (True, -1, "NaN", 901):
            assert client.post("/turn", json={"initial_response_timeout_s": invalid}).status_code == 422
    assert invocations == [300, 120, 30]
    assert effective(30) == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["queue", "preparation", "admission", "model"])
async def test_deadline_records_the_phase_without_fallback(phase):
    closed = []
    async def source():
        try:
            await asyncio.sleep(10)
            yield "unused"
        finally:
            closed.append(True)
    result = [chunk async for chunk in bounded(source(), .01, phase=lambda: phase)]
    event = json.loads(result[0].split("data: ", 1)[1])
    assert event["timeout_phase"] == phase
    assert event["fallback_eligible"] is False
    assert event["retryable"] is False
    assert closed == [True]
    if phase == "queue":
        assert "todavia no ha recibido" in event["error"]


@pytest.mark.asyncio
async def test_reasoning_delta_starts_response_and_does_not_cut_long_generation():
    async def source():
        yield 'data: {"delta":"reason", "thinking":true}\n\n'
        await asyncio.sleep(.03)
        yield 'data: {"delta":"answer"}\n\n'
    result = [chunk async for chunk in bounded(source(), .01)]
    assert len(result) == 2
    assert "answer" in result[-1]


@pytest.mark.asyncio
async def test_a_source_that_raises_cancelled_reaches_the_consumer():
    """An upstream stream stopped from inside raises CancelledError in the
    source. The consumer must see that cancellation, not wait forever on an
    empty queue (the chat_stream hang of task 359)."""
    closed = []

    async def source():
        try:
            yield "data: partial\n\n"
            raise asyncio.CancelledError()
        finally:
            closed.append(True)

    got = []

    async def consume():
        async for chunk in scoped_stream(source(), None):
            got.append(chunk)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(consume(), timeout=5)
    assert got == ["data: partial\n\n"]
    assert closed == [True]


@pytest.mark.asyncio
async def test_closing_the_consumer_still_cancels_the_producer_quietly():
    started, closed = asyncio.Event(), []

    async def source():
        try:
            yield "first"
            started.set()
            await asyncio.Event().wait()   # an upstream that never answers
            yield "never"
        finally:
            closed.append(True)

    stream = scoped_stream(source(), None)
    assert await stream.__anext__() == "first"
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.wait_for(stream.aclose(), timeout=5)
    assert closed == [True]


@pytest.mark.asyncio
async def test_errors_and_the_end_still_pass_through():
    async def failing():
        yield "a"
        raise RuntimeError("upstream broke")

    got = []
    with pytest.raises(RuntimeError, match="upstream broke"):
        async for chunk in scoped_stream(failing(), None):
            got.append(chunk)
    assert got == ["a"]

    async def finite():
        yield "x"
        yield "y"

    assert [c async for c in scoped_stream(finite(), None)] == ["x", "y"]
