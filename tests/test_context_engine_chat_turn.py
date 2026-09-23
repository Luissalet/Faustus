"""Context Engine: plain chat (mode "chat") through the live packet.

Plain chat calls `stream_llm_with_fallback` directly and never reached the
agent loop's `deliver_round`. With `agent_context_engine` on it now gets one
packet per turn (`wiring.deliver_chat_turn`): the preface's saved-memory and
document blocks leave when a packet arrives, the packet goes right before the
user's message (never into the system prefix, which must stay byte-stable for
the backend's prompt cache), a failure falls back to the preface untouched,
and the packet is receipted once with the turn's outcome (consumer "chat").
Flag off: nothing changes.
"""

from __future__ import annotations

import json

import pytest

import routes.chat_routes as chat_routes
from src.context_engine import compiler as ce_compiler
from src.context_engine import standby
from src.context_engine import wiring
from src.prompt_security import untrusted_context_message
from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint

PINNED_TEXT = "Ada prefers green tea in the morning."
DOC_TEXT = "Bluehaven handbook: deployments run on Fridays."


def _messages(tagged: bool):
    messages = [
        {"role": "system", "content": "UNTRUSTED CONTEXT POLICY"},
        untrusted_context_message("saved memory: pinned context", PINNED_TEXT),
        untrusted_context_message("retrieved documents", DOC_TEXT),
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "_agent_injected": "context", "_reply_language": "es",
         "content": "[Runtime requirement — reply language] Spanish"},
        {"role": "user", "content": "hello"},
    ]
    if tagged:
        standby.mark_preface_standby(messages)
    return messages


def _packet(packet_id="ctxpkt_chat"):
    return {
        "message": {"role": "user", "content": "compiled packet body",
                    "_agent_injected": "context_engine",
                    "metadata": {"trusted": False, "context_packet_id": packet_id}},
        "report": {"packet_id": packet_id, "request_id": "ctxreq_chat", "delivered": True,
                   "sources": [
                       {"section": "memory", "source_type": "memory", "source_ref": "pmem:ada-1"},
                       {"section": "memory", "source_type": "memory", "source_ref": "pmem:ada-1"},
                       {"section": "retrieved_documents", "source_type": "document",
                        "source_ref": "doc:handbook#chunk1"},
                   ],
                   "recallable": []},
    }


def _blocks(messages, text):
    return [m for m in messages if text in str(m.get("content") or "")]


def _packets(messages):
    return [m for m in messages if m.get("_agent_injected") == "context_engine"]


@pytest.fixture()
def scripted_delivery(monkeypatch):
    seen = {"requests": [], "outcomes": []}

    async def fake_deliver(**kwargs):
        seen["requests"].append(kwargs)
        outcome = seen["outcomes"].pop(0) if seen["outcomes"] else None
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(wiring, "deliver_round", fake_deliver)
    return seen


# ── wiring.deliver_chat_turn ───────────────────────────────────────────────

async def _deliver(messages):
    return await wiring.deliver_chat_turn(
        messages=messages, owner="ada", session_id="s-ada", model="m",
        context_length=8192, window_known=True, max_output_tokens=512)


@pytest.mark.asyncio
async def test_flag_off_returns_the_prompt_unchanged(monkeypatch, scripted_delivery):
    monkeypatch.setattr(wiring, "enabled", lambda: False)
    original = _messages(tagged=False)
    out, delivered = await _deliver(original)
    assert delivered is None and out == original
    assert scripted_delivery["requests"] == []


@pytest.mark.asyncio
async def test_packet_replaces_the_standby_blocks_before_the_question(monkeypatch,
                                                                     scripted_delivery):
    monkeypatch.setattr(wiring, "enabled", lambda: True)
    scripted_delivery["outcomes"] = [_packet()]
    original = _messages(tagged=True)
    out, delivered = await _deliver(original)
    assert delivered and delivered["report"]["packet_id"] == "ctxpkt_chat"
    assert _blocks(out, PINNED_TEXT) == [] and _blocks(out, DOC_TEXT) == []
    # before the reply-language directive, which stays last before the user
    assert [m.get("content") for m in out][-3:] == [
        "compiled packet body", "[Runtime requirement — reply language] Spanish", "hello"]
    # byte-stable prefix: system content untouched, the packet is never system
    assert out[0] == original[0]
    assert all(m.get("role") != "system" for m in _packets(out))
    # the packet was budgeted without the blocks it replaces
    request_messages = scripted_delivery["requests"][0]["messages"]
    assert not any(standby.is_standby(m) for m in request_messages)
    request = scripted_delivery["requests"][0]["request"]
    assert request.consumer == "chat" and request.actor.role == "chat"
    assert request.task.query == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [None, RuntimeError("store down")])
async def test_failed_delivery_keeps_the_preface_exactly_once(monkeypatch, scripted_delivery,
                                                              outcome):
    monkeypatch.setattr(wiring, "enabled", lambda: True)
    scripted_delivery["outcomes"] = [outcome]
    original = _messages(tagged=True)
    out, delivered = await _deliver(original)
    assert delivered is None and out == original
    assert len(_blocks(out, PINNED_TEXT)) == 1 and len(_blocks(out, DOC_TEXT)) == 1


def test_receipt_rows_are_deduplicated():
    rows = wiring.receipt_rows(_packet()["report"])
    assert [(r["kind"], r["ref"]) for r in rows] == [
        ("memory", "pmem:ada-1"), ("document", "doc:handbook#chunk1")]


def test_observe_receipt_records_the_consumer(monkeypatch):
    recorded = []
    monkeypatch.setattr(ce_compiler, "record_receipt", recorded.append)
    wiring.observe_receipt(packet_id="ctxpkt_chat", verdict="complete", consumer="chat")
    wiring.observe_receipt(packet_id="ctxpkt_agent", verdict="complete")
    wiring.observe_receipt(packet_id="ctxpkt_odd", verdict="complete", consumer="nope")
    assert [r.consumer for r in recorded] == ["chat", "agent", "agent"]


# ── the chat route ─────────────────────────────────────────────────────────

def _route(monkeypatch, *, engine_on, chunks=None):
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured, capture_completion=True)
    monkeypatch.setattr(wiring, "enabled", lambda: engine_on)
    original_build = chat_routes.build_chat_context

    async def build_with_preface(*args, **kwargs):
        ctx = await original_build(*args, **kwargs)
        ctx.messages = _messages(tagged=engine_on)
        ctx.route_messages = _messages(tagged=engine_on)
        ctx.used_memories = [{"text": PINNED_TEXT, "type": "pinned"}]
        ctx.rag_sources = [{"filename": "handbook.md"}]
        return ctx

    monkeypatch.setattr(chat_routes, "build_chat_context", build_with_preface)
    sent = []

    async def fake_stream(candidates, messages, **kwargs):
        sent.append([dict(m) for m in messages])
        for chunk in chunks or (f'data: {json.dumps({"delta": "done"})}\n\n',
                                "data: [DONE]\n\n"):
            yield chunk

    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", fake_stream)
    receipts = []
    monkeypatch.setattr(wiring, "observe_receipt", lambda **kw: receipts.append(kw))
    return endpoint, captured, sent, receipts


async def _drain(endpoint):
    response = await endpoint(_RouteRequest("chat"))
    events = []
    async for chunk in response.body_iterator:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        if text.startswith("data: {"):
            try:
                events.append(json.loads(text[6:]))
            except ValueError:
                pass
    return events


@pytest.mark.asyncio
async def test_route_flag_off_is_unchanged(monkeypatch, scripted_delivery):
    endpoint, captured, sent, receipts = _route(monkeypatch, engine_on=False)
    events = await _drain(endpoint)
    assert scripted_delivery["requests"] == []
    assert len(_blocks(sent[0], PINNED_TEXT)) == 1 and _packets(sent[0]) == []
    assert not any(e.get("type") == "context_packet" for e in events)
    assert receipts == []
    saved_kwargs = captured["saved"][0][1]
    assert saved_kwargs["used_memories"] and saved_kwargs["rag_sources"]


@pytest.mark.asyncio
async def test_route_flag_on_sends_the_packet_and_receipts_it(monkeypatch, scripted_delivery):
    endpoint, captured, sent, receipts = _route(monkeypatch, engine_on=True)
    scripted_delivery["outcomes"] = [_packet()]
    events = await _drain(endpoint)
    assert len(scripted_delivery["requests"]) == 1, "one packet per chat turn"
    assert _blocks(sent[0], PINNED_TEXT) == [] and _blocks(sent[0], DOC_TEXT) == []
    assert len(_packets(sent[0])) == 1
    assert [e["data"]["packet_id"] for e in events if e.get("type") == "context_packet"] == [
        "ctxpkt_chat"]
    assert [(r["packet_id"], r["verdict"], r["consumer"]) for r in receipts] == [
        ("ctxpkt_chat", "complete", "chat")]
    saved_args, saved_kwargs = captured["saved"][0]
    # the preface blocks were not sent, so they are not claimed as used
    assert saved_kwargs["used_memories"] is None and saved_kwargs["rag_sources"] is None
    assert [r["ref"] for r in saved_args[4]["context_receipts"]] == [
        "pmem:ada-1", "doc:handbook#chunk1"]


@pytest.mark.asyncio
async def test_route_flag_on_failure_falls_back_to_the_preface(monkeypatch, scripted_delivery):
    endpoint, captured, sent, receipts = _route(monkeypatch, engine_on=True)
    scripted_delivery["outcomes"] = [None]
    await _drain(endpoint)
    assert len(_blocks(sent[0], PINNED_TEXT)) == 1 and len(_blocks(sent[0], DOC_TEXT)) == 1
    assert _packets(sent[0]) == []
    assert receipts == [], "no packet, no receipt"
    assert captured["saved"][0][1]["used_memories"]


@pytest.mark.asyncio
async def test_route_terminal_error_receipts_the_packet_as_error(monkeypatch, scripted_delivery):
    endpoint, captured, sent, receipts = _route(
        monkeypatch, engine_on=True,
        chunks=(f'data: {json.dumps({"delta": "partial"})}\n\n',
                'event: error\ndata: {"status": 503}\n\n'))
    scripted_delivery["outcomes"] = [_packet("ctxpkt_err")]
    await _drain(endpoint)
    assert [(r["packet_id"], r["verdict"]) for r in receipts] == [("ctxpkt_err", "error")]
