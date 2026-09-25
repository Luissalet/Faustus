"""A client that leaves after the answer was saved does not save it twice.

Seen live: a talk client read the approval card ("Allow this task to
continue?"), closed the stream, and the transcript held the card twice — once
as the saved answer and once as a "stopped" partial written by the
disconnect handler. The disconnect handler now only saves a partial for a
turn that had not already been saved.
"""
import asyncio
import json

import pytest

import routes.chat_routes as chat_routes
from tests.test_lote20_chat_routes_budget_exhausted import _RouteRequest, _chat_stream_endpoint

_CARD = "Allow this task to continue?"
_CANCEL = object()


def _cancelled(*a, **k):
    # The run is cancelled while the route is still busy after the save
    # (live: the approval continuation replaced the stream holding the card).
    raise asyncio.CancelledError()


async def _run(chunks, stop_on="\0", cancel_after_save=False):
    monkeypatch = pytest.MonkeyPatch()
    saves, partials = [], []
    try:
        endpoint = _chat_stream_endpoint(monkeypatch, chunks)

        async def _agent(endpoint_url, model, messages, **kwargs):
            for chunk in chunks:
                if chunk is _CANCEL:
                    raise asyncio.CancelledError()
                yield chunk

        monkeypatch.setattr(chat_routes, "stream_agent_loop", _agent)
        monkeypatch.setattr(chat_routes, "save_assistant_response",
                            lambda *a, **k: saves.append(a[3]) or "m1")

        def _partial(text, meta):
            partials.append(text)
            return text, meta

        monkeypatch.setattr(chat_routes, "clean_thinking_for_save", _partial)
        if cancel_after_save:
            monkeypatch.setattr(chat_routes, "run_post_response_tasks", _cancelled)
        response = await endpoint(_RouteRequest())
        it = response.body_iterator
        try:
            async for chunk in it:
                if stop_on in chunk:
                    break
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await it.aclose()
    finally:
        monkeypatch.undo()
    return saves, partials


@pytest.mark.asyncio
async def test_closing_after_the_saved_answer_does_not_save_a_partial():
    chunks = ['data: ' + json.dumps({"delta": _CARD}) + '\n\n', "data: [DONE]\n\n"]
    saves, partials = await _run(chunks, cancel_after_save=True)
    assert saves == [_CARD]
    assert partials == []


@pytest.mark.asyncio
async def test_closing_mid_answer_still_saves_the_partial():
    chunks = ['data: ' + json.dumps({"delta": "half an ans"}) + '\n\n', _CANCEL]
    saves, partials = await _run(chunks)
    assert saves == []
    assert partials and partials[0].startswith("half an ans")
