"""tests/test_w3_doc_context.py — W3-INT (CONTRATO_CMP_W2.md § W2-A1/CMP-03).

`Composer.tsx`'s "Sobre esta selección…" chips travel to the server as
`doc_context` (`adapters/chat.ts::SendOptions.docContext`, forwarded by
`Studio.tsx`'s `sendTurn` call) — read here for the first time:
`routes/chat_routes.py`'s `/api/chat_stream` parses the JSON payload, checks
the referenced document actually belongs to the caller, and inserts one
`untrusted_context_message` per document into the turn right before the
current user message. The model reads the selected fragment as CONTEXT it
was told, never as text the user typed, and it never by itself authorizes
editing anything — that still needs an ordinary tool call through the usual
approval gate.

Driven through the real `/api/chat_stream` route function via the
`_chat_stream_endpoint` fake-harness `tests/test_foreground_model_routing.py`
already builds (the same pattern `tests/test_tool_approval_gate_error_contract.py`
uses), with a real disposable sqlite `Document` store swapped in for
`chat_routes.SessionLocal` so document ownership is checked for real instead
of faked.

Run: python3 -m pytest tests/test_w3_doc_context.py -q -p no:cacheprovider -W ignore
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core.database as cdb  # noqa: E402
from core.database import Document  # noqa: E402
import routes.chat_routes as chat_routes  # noqa: E402

from test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint  # noqa: E402

OWNER = "alice"  # _chat_stream_endpoint's fake `effective_user` always answers "alice"
OTHER = "mallory"


@pytest.fixture()
def db():
    """A real, disposable sqlite `Document` store — `chat_routes._doc_context_messages`
    reaches it via `SessionLocal()`. NOT patched into `chat_routes` here: each
    test calls `_chat_stream_endpoint` first (which points `SessionLocal` at
    its own always-empty fake) and patches this in afterwards, or the fake
    would win (same `monkeypatch`, last write wins)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    engine = create_engine(f"sqlite:///{tmp.name}", connect_args={"check_same_thread": False}, poolclass=NullPool)
    cdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _make_document(db, *, owner=OWNER, title="Doc", content="hello world\n"):
    doc_id = str(uuid.uuid4())
    session = db()
    try:
        session.add(Document(id=doc_id, owner=owner, title=title, current_content=content, version_count=1))
        session.commit()
    finally:
        session.close()
    return doc_id


def _doc_context_form(doc_id, *, quote="hello", start=0, end=5):
    return json.dumps([{
        "docId": doc_id,
        "docTitle": "client-supplied title — the server must use the real one",
        "ranges": [{"start": start, "end": end}],
        "action": "clarify",
        "quotes": [quote],
    }])


def _capture_chat_stream(sink):
    async def _fake(candidates, messages, **kwargs):
        sink["messages"] = messages
        yield f'data: {json.dumps({"delta": "done"})}\n\n'
        yield "data: [DONE]\n\n"
    return _fake


# ---------------------------------------------------------------------------
# The decisive case: an owned document's selection becomes a context message
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_doc_context_injects_a_context_message_for_owned_document(monkeypatch, db):
    doc_id = _make_document(db, title="Meeting notes")
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    # `_chat_stream_endpoint` itself points `chat_routes.SessionLocal` at a
    # fake that always answers "not found" (`_EmptyDb`) — re-point it at the
    # real sqlite store AFTER that call, or this override is the one that
    # wins (same `monkeypatch`, last write wins).
    monkeypatch.setattr(chat_routes, "SessionLocal", db)
    sink = {}
    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", _capture_chat_stream(sink))

    request = _RouteRequest("chat")
    request._form.update({"doc_context": _doc_context_form(doc_id, quote="hello")})

    response = await endpoint(request)
    async for _ in response.body_iterator:
        pass

    messages = sink["messages"]
    context_messages = [m for m in messages if "Meeting notes" in str(m.get("content"))]
    assert context_messages, messages
    msg = context_messages[0]
    assert msg["role"] == "user"  # never system: it is data the model reads, not an instruction
    assert "hello" in msg["content"]
    assert "0" in msg["content"] and "5" in msg["content"]  # the range is named
    # untrusted_context_message's own contract: never presented as trusted.
    assert msg.get("metadata", {}).get("trusted") is False

    # It sits right before the actual current turn, not buried or pushed to
    # the very front ahead of the system preface.
    assert messages[-1]["content"] == "hello"  # _RouteRequest's own turn text
    assert messages[messages.index(msg) + 1] is messages[-1] or len(messages) == 1


# ---------------------------------------------------------------------------
# Never authorizes anything by itself: role stays "user"/untrusted content,
# never folded into a system message or a tool call.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_doc_context_is_never_placed_in_system_role(monkeypatch, db):
    doc_id = _make_document(db, title="Contract draft")
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    # `_chat_stream_endpoint` itself points `chat_routes.SessionLocal` at a
    # fake that always answers "not found" (`_EmptyDb`) — re-point it at the
    # real sqlite store AFTER that call, or this override is the one that
    # wins (same `monkeypatch`, last write wins).
    monkeypatch.setattr(chat_routes, "SessionLocal", db)
    sink = {}
    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", _capture_chat_stream(sink))

    request = _RouteRequest("chat")
    request._form.update({"doc_context": _doc_context_form(doc_id, quote="clause 4")})

    response = await endpoint(request)
    async for _ in response.body_iterator:
        pass

    for m in sink["messages"]:
        if "Contract draft" in str(m.get("content")):
            assert m["role"] != "system"


# ---------------------------------------------------------------------------
# Owner check: a document belonging to someone else never becomes context.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_doc_context_for_another_owners_document_is_dropped(monkeypatch, db):
    doc_id = _make_document(db, owner=OTHER, title="Not yours")
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    # `_chat_stream_endpoint` itself points `chat_routes.SessionLocal` at a
    # fake that always answers "not found" (`_EmptyDb`) — re-point it at the
    # real sqlite store AFTER that call, or this override is the one that
    # wins (same `monkeypatch`, last write wins).
    monkeypatch.setattr(chat_routes, "SessionLocal", db)
    sink = {}
    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", _capture_chat_stream(sink))

    request = _RouteRequest("chat")
    request._form.update({"doc_context": _doc_context_form(doc_id)})

    response = await endpoint(request)
    async for _ in response.body_iterator:
        pass

    assert not any("Not yours" in str(m.get("content")) for m in sink["messages"])


# ---------------------------------------------------------------------------
# A nonexistent document id is dropped the same way, silently.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_doc_context_for_missing_document_is_dropped(monkeypatch, db):
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    # `_chat_stream_endpoint` itself points `chat_routes.SessionLocal` at a
    # fake that always answers "not found" (`_EmptyDb`) — re-point it at the
    # real sqlite store AFTER that call, or this override is the one that
    # wins (same `monkeypatch`, last write wins).
    monkeypatch.setattr(chat_routes, "SessionLocal", db)
    sink = {}
    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", _capture_chat_stream(sink))

    request = _RouteRequest("chat")
    request._form.update({"doc_context": _doc_context_form("no-such-document-id")})

    response = await endpoint(request)
    async for _ in response.body_iterator:
        pass

    assert sink["messages"] == [{"role": "user", "content": "hello"}]


# ---------------------------------------------------------------------------
# Malformed payloads never fail the turn — same "additive" posture as
# `contextOverrides`'s own doc comment describes for an unrecognized field.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_doc_context_malformed_payload_never_fails_the_turn(monkeypatch, db):
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    request = _RouteRequest("chat")
    request._form.update({"doc_context": "not json at all"})

    response = await endpoint(request)
    chunks = [chunk async for chunk in response.body_iterator]
    assert chunks[-1] == "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# No doc_context sent at all: today's existing behaviour, byte for byte.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_doc_context_field_is_unchanged(monkeypatch, db):
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    # `_chat_stream_endpoint` itself points `chat_routes.SessionLocal` at a
    # fake that always answers "not found" (`_EmptyDb`) — re-point it at the
    # real sqlite store AFTER that call, or this override is the one that
    # wins (same `monkeypatch`, last write wins).
    monkeypatch.setattr(chat_routes, "SessionLocal", db)
    sink = {}
    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", _capture_chat_stream(sink))

    response = await endpoint(_RouteRequest("chat"))
    async for _ in response.body_iterator:
        pass

    assert sink["messages"] == [{"role": "user", "content": "hello"}]
