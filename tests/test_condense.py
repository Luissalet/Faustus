"""Tests for manual condense of a turn range — CONTRATO_CABLES2.md F3, Lote A.

Same harness `tests/test_side_threads.py` uses: a real `SessionManager` over
a temp file-backed SQLite DB, not a mock — `condense()`/`expand()` persist
through `SessionManager.replace_messages`, and a `SimpleNamespace` double
cannot stand in for that.

No real LLM anywhere in this module: `no_real_llm` (autouse) makes
`src.context_compactor.llm_call_async` raise if a test forgets to patch it
with a fake summary, so a test that expects "no LLM call at all" (every
`preview`) genuinely proves that, and every `condense` test controls exactly
what the "model" returns.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from core.models import ChatMessage


# ---------------------------------------------------------------------------
# DB / SessionManager / TestClient harness (mirrors tests/test_side_threads.py)
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    import core.database as db_mod
    import core.session_manager as sm_mod
    import routes.session_routes as sr_mod

    url = "sqlite:///" + (tmp_path / "condense.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    monkeypatch.setattr(sm_mod, "SessionLocal", Session)
    monkeypatch.setattr(sr_mod, "SessionLocal", Session)
    yield Session
    engine.dispose()


@pytest.fixture()
def sm(db):
    from core.session_manager import SessionManager
    return SessionManager()


def _fake_effective_user(request):
    return request.headers.get("x-test-user", "alice")


@pytest.fixture()
def client(db, sm, monkeypatch):
    import routes.session_routes as sr_mod
    import routes.condense_routes as cr_mod

    monkeypatch.setattr(sr_mod, "effective_user", _fake_effective_user)
    monkeypatch.setattr(cr_mod, "effective_user", _fake_effective_user)

    app = FastAPI()
    app.include_router(cr_mod.setup_condense_routes(sm))
    with TestClient(app) as c:
        yield c


def _hdr(user):
    return {"x-test-user": user} if user else {}


def _mk_session(sm, sid, *, owner="alice", name="Main", n_messages=0):
    sm.create_session(session_id=sid, name=name, endpoint_url="http://ep", model="m1", owner=owner)
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        sm.add_message(sid, ChatMessage(role, f"msg-{i}"))
    return sm.get_session(sid)


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """Fail loudly if a test path ever falls through to a real model call —
    every `condense()` test below patches `cc.llm_call_async` itself with a
    fixed fake; `preview()` must never reach this at all."""
    import src.context_compactor as cc

    def _boom(*args, **kwargs):
        raise AssertionError("no real LLM call is allowed in this test suite")

    monkeypatch.setattr(cc, "llm_call_async", _boom)
    monkeypatch.setattr(cc, "resolve_endpoint", lambda which, owner=None: (None, None, None))
    yield


def _fake_summary(monkeypatch, text="SUMMARY TEXT"):
    import src.context_compactor as cc

    async def _fake(*args, **kwargs):
        return text

    monkeypatch.setattr(cc, "llm_call_async", _fake)


# ---------------------------------------------------------------------------
# preview — no LLM call, pure range validation + reporting
# ---------------------------------------------------------------------------

def test_preview_shape_and_no_llm_call(sm):
    from src.condense import preview

    _mk_session(sm, "s1", n_messages=6)  # msg-0..msg-5
    result = preview(sm, "alice", "s1", 1, 3)
    assert result["rows"] == 3
    assert result["tokens_before"] > 0
    assert result["tokens_after_estimate"] <= result["tokens_before"]
    assert [t["index"] for t in result["turns"]] == [1, 2, 3]
    assert [t["role"] for t in result["turns"]] == ["assistant", "user", "assistant"]
    assert result["turns"][0]["excerpt"] == "msg-1"


def test_preview_last_row_is_always_protected(sm):
    from src.condense import preview, CondenseError

    _mk_session(sm, "s1", n_messages=4)  # msg-0..msg-3, last index 3
    with pytest.raises(CondenseError) as excinfo:
        preview(sm, "alice", "s1", 2, 3)  # end must be <= len-2 == 2
    assert excinfo.value.error_class == "condense.range_invalid"
    assert excinfo.value.status == 400
    # end == len-2 (2) is fine.
    ok = preview(sm, "alice", "s1", 0, 2)
    assert ok["rows"] == 3


def test_preview_range_bounds(sm):
    from src.condense import preview, CondenseError

    _mk_session(sm, "s1", n_messages=6)
    for start, end in [(-1, 2), (2, 1), (1, 1), (0, 10)]:
        with pytest.raises(CondenseError) as excinfo:
            preview(sm, "alice", "s1", start, end)
        assert excinfo.value.error_class == "condense.range_invalid"


def test_preview_owner_scoping_is_404(sm):
    from src.condense import preview, CondenseError

    _mk_session(sm, "theirs", owner="bob", n_messages=4)
    with pytest.raises(CondenseError) as excinfo:
        preview(sm, "alice", "theirs", 0, 1)
    assert excinfo.value.error_class == "condense.not_found"
    assert excinfo.value.status == 404


def test_preview_rejects_a_range_that_contains_an_already_condensed_row(sm, monkeypatch):
    import asyncio
    from src.condense import condense, preview, CondenseError

    _mk_session(sm, "s1", n_messages=8)  # msg-0..msg-7
    _fake_summary(monkeypatch)
    asyncio.run(condense(sm, "alice", "s1", 1, 3))
    # History is now: msg-0, [condensed], msg-4..msg-7 (6 rows, condensed at index 1).
    with pytest.raises(CondenseError) as excinfo:
        preview(sm, "alice", "s1", 0, 2)  # range [0,2] includes the condensed row at 1
    assert excinfo.value.error_class == "condense.nested"
    assert excinfo.value.status == 409
    assert "expand" in str(excinfo.value)


# ---------------------------------------------------------------------------
# condense — replaces N rows with 1, keeps an exact undo record
# ---------------------------------------------------------------------------

def test_condense_replaces_the_range_with_one_row_and_keeps_condensed_from(sm, monkeypatch):
    import asyncio
    from src.condense import condense

    sess = _mk_session(sm, "s1", n_messages=6)  # msg-0..msg-5
    original = [(m.role, m.content) for m in sess.history]
    _fake_summary(monkeypatch, text="the gist of turns 2-4")

    result = asyncio.run(condense(sm, "alice", "s1", 1, 3))
    assert result == {
        "summary_index": 1,
        "removed": 2,  # 3 rows -> 1
        "tokens_before": result["tokens_before"],
        "tokens_after": result["tokens_after"],
    }
    assert result["tokens_before"] > 0
    assert result["tokens_after"] > 0

    new_sess = sm.get_session("s1")
    assert len(new_sess.history) == 4  # 6 - 3 + 1
    summary_row = new_sess.history[1]
    assert summary_row.role == "system"
    assert summary_row.content == "[Condensed: turns 2–4]\nthe gist of turns 2-4"
    meta = summary_row.metadata
    assert meta["condensed"] is True
    assert meta["range"] == [1, 3]
    assert meta["model"] == "m1"
    assert [(e["role"], e["content"]) for e in meta["condensed_from"]] == original[1:4]
    # Untouched rows keep their exact place either side of the summary.
    assert (new_sess.history[0].role, new_sess.history[0].content) == original[0]
    assert (new_sess.history[2].role, new_sess.history[2].content) == original[4]
    assert (new_sess.history[3].role, new_sess.history[3].content) == original[5]


def test_condense_last_row_protected(sm, monkeypatch):
    import asyncio
    from src.condense import condense, CondenseError

    _mk_session(sm, "s1", n_messages=4)
    _fake_summary(monkeypatch)
    with pytest.raises(CondenseError) as excinfo:
        asyncio.run(condense(sm, "alice", "s1", 2, 3))
    assert excinfo.value.error_class == "condense.range_invalid"


def test_condense_owner_scoping_is_404(sm, monkeypatch):
    import asyncio
    from src.condense import condense, CondenseError

    _mk_session(sm, "theirs", owner="bob", n_messages=6)
    _fake_summary(monkeypatch)
    with pytest.raises(CondenseError) as excinfo:
        asyncio.run(condense(sm, "alice", "theirs", 0, 2))
    assert excinfo.value.error_class == "condense.not_found"


def test_condense_summary_failure_is_a_flat_error_not_a_silent_degrade(sm, monkeypatch):
    """Unlike `maybe_compact` (a background best-effort that quietly keeps
    the conversation uncompacted on a model failure), a manual condense is
    an explicit user action — a failed summary must surface as an error the
    route can report, never a silent no-op."""
    import asyncio
    import src.context_compactor as cc
    from src.condense import condense, CondenseError

    _mk_session(sm, "s1", n_messages=6)

    async def _fail(*a, **k):
        raise RuntimeError("endpoint unreachable")

    monkeypatch.setattr(cc, "llm_call_async", _fail)
    with pytest.raises(CondenseError) as excinfo:
        asyncio.run(condense(sm, "alice", "s1", 1, 3))
    assert excinfo.value.error_class == "condense.summary_failed"

    # And the history was never touched.
    assert len(sm.get_session("s1").history) == 6


# ---------------------------------------------------------------------------
# expand — restores byte for byte
# ---------------------------------------------------------------------------

def _without_db_id(meta):
    if not isinstance(meta, dict):
        return meta
    return {k: v for k, v in meta.items() if k != "_db_id"}


def test_expand_restores_the_range_byte_for_byte(sm, monkeypatch):
    """"Byte for byte" is role/content/metadata — `_db_id` is
    `SessionManager.replace_messages`'s own bookkeeping, freshly reissued
    for EVERY row on every replace (untouched rows included, since a
    replace always rewrites the whole table), never part of a message's
    actual content. `condense()` already drops it from `condensed_from` for
    exactly this reason; this test does the same on both sides of the
    comparison."""
    import asyncio
    from src.condense import condense, expand

    sess = _mk_session(sm, "s1", n_messages=6)
    for m in sess.history:
        m.metadata = {"some": "meta", "for": m.content}
    sm.replace_messages("s1", sess.history)
    original = [(m.role, m.content, _without_db_id(m.metadata)) for m in sm.get_session("s1").history]

    _fake_summary(monkeypatch)
    asyncio.run(condense(sm, "alice", "s1", 1, 3))

    result = expand(sm, "alice", "s1", 1)
    assert result == {"restored": 3}

    restored_history = sm.get_session("s1").history
    restored = [(m.role, m.content, _without_db_id(m.metadata)) for m in restored_history]
    assert restored == original


def test_expand_not_condensed_is_404(sm):
    from src.condense import expand, CondenseError

    _mk_session(sm, "s1", n_messages=4)
    with pytest.raises(CondenseError) as excinfo:
        expand(sm, "alice", "s1", 0)
    assert excinfo.value.error_class == "condense.not_condensed"
    assert excinfo.value.status == 404


def test_expand_owner_scoping_is_404(sm, monkeypatch):
    import asyncio
    from src.condense import condense, expand, CondenseError

    _mk_session(sm, "theirs", owner="bob", n_messages=6)
    _fake_summary(monkeypatch)
    asyncio.run(condense(sm, "bob", "theirs", 1, 3))
    with pytest.raises(CondenseError) as excinfo:
        expand(sm, "alice", "theirs", 1)
    assert excinfo.value.error_class == "condense.not_found"


# ---------------------------------------------------------------------------
# Routes — JSON shapes, errors
# ---------------------------------------------------------------------------

def test_condense_routes_end_to_end(client, sm, monkeypatch):
    _mk_session(sm, "s1", n_messages=6)
    _fake_summary(monkeypatch, text="round trip summary")

    r = client.get("/api/session/s1/condense/preview", params={"start": 1, "end": 3}, headers=_hdr("alice"))
    assert r.status_code == 200
    assert set(r.json()) == {"rows", "tokens_before", "tokens_after_estimate", "turns"}

    r2 = client.post("/api/session/s1/condense", json={"start": 1, "end": 3}, headers=_hdr("alice"))
    assert r2.status_code == 200
    body = r2.json()
    assert set(body) == {"summary_index", "removed", "tokens_before", "tokens_after"}
    assert body["summary_index"] == 1
    assert body["removed"] == 2

    r3 = client.post("/api/session/s1/condense/1/expand", headers=_hdr("alice"))
    assert r3.status_code == 200
    assert r3.json() == {"restored": 3}


def test_condense_route_owner_scoping_is_404(client, sm):
    _mk_session(sm, "theirs", owner="bob", n_messages=6)
    r = client.get("/api/session/theirs/condense/preview", params={"start": 0, "end": 2}, headers=_hdr("alice"))
    assert r.status_code == 404
    r2 = client.post("/api/session/theirs/condense", json={"start": 0, "end": 2}, headers=_hdr("alice"))
    assert r2.status_code == 404
    r3 = client.post("/api/session/theirs/condense/0/expand", headers=_hdr("alice"))
    assert r3.status_code == 404


def test_condense_route_nested_is_409(client, sm, monkeypatch):
    _mk_session(sm, "s1", n_messages=8)
    _fake_summary(monkeypatch)
    r1 = client.post("/api/session/s1/condense", json={"start": 1, "end": 3}, headers=_hdr("alice"))
    assert r1.status_code == 200

    r2 = client.post("/api/session/s1/condense", json={"start": 0, "end": 2}, headers=_hdr("alice"))
    assert r2.status_code == 409
    assert r2.json()["error_class"] == "condense.nested"


def test_condense_strips_the_compactor_bookkeeping_line():
    from src.condense import _strip_template_header

    raw = "**Turns summarized:** 8  |  **Compactions so far:** 1\n\n### User Goal\nShip it."
    assert _strip_template_header(raw) == "### User Goal\nShip it."
    # a summary without the line is untouched
    assert _strip_template_header("### User Goal\nShip it.") == "### User Goal\nShip it."
