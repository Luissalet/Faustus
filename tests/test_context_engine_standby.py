"""Context Engine: the chat preface's saved-memory and document blocks on standby.

With `agent_context_engine` on, the live packet selects saved memory (`pmem:`)
and documents (`doc:`) itself, so the chat preface's own blocks for the same
stores would show the model the same fact twice. They get the learned-memory
treatment (tests/test_context_engine_phase2.py): built as today, dropped by a
model call that carries a packet, kept or restored — same position, same tag —
by one that does not. Flag off: nothing is tagged, nothing moves.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from routes.chat_helpers import build_chat_context
from src import agent_loop as al
from src import memory_engine
from src.context_engine import standby
from src.context_engine import wiring
from src.prompt_security import untrusted_context_message
from tests.test_behavior_mode_injection import _harness

PINNED_TEXT = "Ada prefers green tea in the morning."
RETRIEVED_TEXT = "Bruno works at Cordera Labs."
DOC_TEXT = "Bluehaven handbook: deployments run on Fridays."
QUESTION = "Explica qué hace el fichero server.py"


def _preface():
    return [
        {"role": "system", "content": "UNTRUSTED CONTEXT POLICY"},
        untrusted_context_message("saved memory: pinned context", PINNED_TEXT),
        untrusted_context_message("saved memory: retrieved context", RETRIEVED_TEXT),
        untrusted_context_message("retrieved documents", DOC_TEXT),
        untrusted_context_message("web search results", "a web page"),
    ]


def _blocks(messages, text):
    return [m for m in messages if text in str(m.get("content") or "")]


def _packets(messages):
    return [m for m in messages if m.get("_agent_injected") == "context_engine"]


# ── pure helpers ───────────────────────────────────────────────────────────

def test_only_saved_memory_and_documents_are_tagged():
    preface = _preface()
    assert standby.mark_preface_standby(preface) == 3
    kinds = [standby.standby_kind(m) for m in preface]
    assert kinds == ["", "saved_memory", "saved_memory", "documents", ""]
    assert standby.mark_preface_standby(preface) == 0, "idempotent"


def test_the_learned_memory_check_ignores_preface_standbys():
    preface = _preface()
    standby.mark_preface_standby(preface)
    assert not any(al._is_legacy_memory_standby(m) for m in preface)
    learned = {"role": "user", "content": "x", al._LEGACY_MEMORY_STANDBY_KEY: "learned_memory"}
    assert al._is_legacy_memory_standby(learned)
    assert standby.is_standby(learned) and not standby.is_standby(learned, standby.PREFACE_KINDS)


def test_capture_and_restore_put_blocks_back_in_place():
    messages = _preface() + [{"role": "assistant", "content": "earlier"},
                             {"role": "user", "content": QUESTION}]
    standby.mark_preface_standby(messages)
    original = [m.get("content") for m in messages]
    stash = standby.capture(messages)
    assert len(stash) == 3

    stripped = [dict(m) for m in standby.without_standby(messages)]
    stripped.append({"role": "assistant", "content": "tool call"})
    stripped.append({"role": "tool", "content": "tool output"})
    restored, inserted = standby.restore(stripped, stash)
    assert inserted
    assert [m.get("content") for m in restored][:len(original)] == original
    assert all(standby.is_standby(m) for m in restored[1:4])

    again, inserted_again = standby.restore(restored, stash)
    assert not inserted_again and again == restored, "never duplicated"


def test_restore_without_its_anchor_stays_before_the_question():
    block = untrusted_context_message("retrieved documents", DOC_TEXT)
    standby.mark_preface_standby([block])
    stash = [{"message": block, "after": ("system", "gone"), "index": 9}]
    messages = [{"role": "system", "content": "prompt"},
                {"role": "user", "content": QUESTION}]
    restored, inserted = standby.restore(messages, stash)
    assert inserted
    assert [m["content"] for m in restored][-1] == QUESTION


# ── build_chat_context: tagging only with the flag on ──────────────────────

async def _built_preface(monkeypatch, engine_on):
    sess, request, _ = _harness(monkeypatch)
    monkeypatch.setattr(wiring, "enabled", lambda: engine_on)
    produced = _preface()
    pristine = copy.deepcopy(produced)

    def fake_preface(**kwargs):
        return produced, [], []

    ctx = await build_chat_context(
        sess=sess, request=request, chat_handler=type("H", (), {})(),
        chat_processor=type("P", (), {"build_context_preface": staticmethod(fake_preface)})(),
        message="hello", session_id="s1",
    )
    return ctx, pristine


@pytest.mark.asyncio
async def test_flag_off_leaves_the_preface_byte_identical(monkeypatch):
    ctx, pristine = await _built_preface(monkeypatch, engine_on=False)
    assert ctx.preface == pristine
    assert not any(standby.is_standby(m) for m in ctx.messages)


@pytest.mark.asyncio
async def test_flag_on_tags_the_preface_blocks(monkeypatch):
    ctx, pristine = await _built_preface(monkeypatch, engine_on=True)
    tagged = [m for m in ctx.messages if standby.is_standby(m)]
    assert len(tagged) == 3
    # Same content, same lane: only the private key was added.
    for built, original in zip(ctx.preface, pristine):
        built = {k: v for k, v in built.items() if k != standby.STANDBY_KEY}
        assert built == original


# ── the agent loop ─────────────────────────────────────────────────────────

def _run_turn(monkeypatch, *, engine_on, deliveries=(), rounds=(("Hecho.", "stop"),)):
    from tests.test_agent_harness_loop import _patch_common

    _patch_common(monkeypatch)
    monkeypatch.setattr(memory_engine, "injection_enabled", lambda: False)
    monkeypatch.setattr(wiring, "enabled", lambda: engine_on)
    monkeypatch.setattr(wiring, "shadow_enabled", lambda: False)
    sent = []
    calls = {"n": 0, "deliver": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        sent.append([dict(m) for m in messages])
        index = min(calls["n"], len(rounds) - 1)
        calls["n"] += 1
        text, finish = rounds[index]
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": finish})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)
    script = list(deliveries)

    async def fake_deliver(**kwargs):
        calls["deliver"] += 1
        outcome = script.pop(0) if script else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(wiring, "deliver_round", fake_deliver)
    messages = _preface() + [{"role": "user", "content": QUESTION}]
    if engine_on:
        standby.mark_preface_standby(messages)   # what build_chat_context does
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3.5:9b", messages,
        max_rounds=3, relevant_tools={"read_file"}, session_id="s-ada", owner="ada")

    async def consume():
        out = [chunk async for chunk in gen]
        await gen.aclose()
        return out

    asyncio.run(consume())
    return sent, calls


def _packet(packet_id="ctxpkt_pref"):
    return {
        "message": {"role": "user", "content": "compiled packet body",
                    "_agent_injected": "context_engine",
                    "metadata": {"trusted": False, "context_packet_id": packet_id}},
        "report": {"packet_id": packet_id, "request_id": "ctxreq_1", "delivered": True,
                   "sources": [{"section": "memory", "source_type": "memory",
                                "source_ref": "pmem:ada-1"}],
                   "recallable": []},
    }


def _order(messages):
    return [m.get("content") for m in messages
            if any(t in str(m.get("content") or "")
                   for t in (PINNED_TEXT, RETRIEVED_TEXT, DOC_TEXT, "a web page", QUESTION))]


def test_agent_flag_off_is_unchanged(monkeypatch):
    sent, calls = _run_turn(monkeypatch, engine_on=False)
    assert calls["deliver"] == 0
    for text in (PINNED_TEXT, RETRIEVED_TEXT, DOC_TEXT):
        assert len(_blocks(sent[0], text)) == 1
    assert _packets(sent[0]) == []


def test_agent_packet_replaces_the_preface_blocks(monkeypatch):
    sent, _ = _run_turn(monkeypatch, engine_on=True, deliveries=[_packet()])
    for text in (PINNED_TEXT, RETRIEVED_TEXT, DOC_TEXT):
        assert _blocks(sent[0], text) == [], "never both"
    assert len(_blocks(sent[0], "a web page")) == 1, "web results are not on standby"
    assert len(_packets(sent[0])) == 1


@pytest.mark.parametrize("outcome", [None, RuntimeError("store down")])
def test_agent_failed_packet_keeps_the_blocks_exactly_once(monkeypatch, outcome):
    sent, _ = _run_turn(monkeypatch, engine_on=True, deliveries=[outcome])
    for text in (PINNED_TEXT, RETRIEVED_TEXT, DOC_TEXT):
        assert len(_blocks(sent[0], text)) == 1
    assert _packets(sent[0]) == []


def test_agent_packet_then_none_restores_the_blocks_in_place(monkeypatch):
    baseline, _ = _run_turn(monkeypatch, engine_on=False)
    sent, _ = _run_turn(
        monkeypatch, engine_on=True, deliveries=[_packet(), None, None],
        rounds=(("Esta es la primera mitad de la explicación del fichero", "length"),
                ("y este es el resto de la explicación.", "stop")))
    assert len(sent) >= 2
    assert _blocks(sent[0], PINNED_TEXT) == []
    for text in (PINNED_TEXT, RETRIEVED_TEXT, DOC_TEXT):
        assert len(_blocks(sent[1], text)) == 1, "back exactly once"
    assert _packets(sent[1]) == []
    assert _order(sent[1]) == _order(baseline[0]), "same position as the legacy prompt"
