"""Tests for excursos (side threads) — CONTRATO_EXCURSOS.md, Lote A.

Real `SessionManager` over a temp file-backed SQLite DB
(`tests/test_project_identity.py`'s pattern), not a mock: `src/side_threads.py`
mixes DB-only functions (`add_reference`, `wires_for`, `thought_map`,
`parents_map`) with `SessionManager`-backed ones (`create_side_thread`,
`inherited_context`, `context_preview`), so a `SimpleNamespace` double can't
stand in for real anchor-index slicing against a real `chat_messages` table
and real FK-cascade behaviour.

No LLM is ever involved — `inherited_context` (the hook) is exercised
directly, exactly as `routes/chat_helpers.py::build_chat_context` calls it,
never through a real model request.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from core.models import ChatMessage
from src.context_compactor import HISTORY_INDEX_KEY


# ---------------------------------------------------------------------------
# DB / SessionManager / TestClient harness
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A file-backed sqlite DB bound everywhere the code under test reads it
    (`core.database`, `core.session_manager`, `routes.session_routes` all
    import `SessionLocal` by name at import time, so each has to be patched
    individually — see `tests/test_project_identity.py`)."""
    import core.database as db_mod
    import core.session_manager as sm_mod
    import routes.session_routes as sr_mod
    import src.side_threads as st_mod

    url = "sqlite:///" + (tmp_path / "side_threads.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    monkeypatch.setattr(sm_mod, "SessionLocal", Session)
    monkeypatch.setattr(sr_mod, "SessionLocal", Session)
    # src/side_threads.py does `from core.database import SessionLocal`, its
    # own name binding at import time — patching core.database.SessionLocal
    # alone does not redirect it, so it needs its own patch too.
    monkeypatch.setattr(st_mod, "SessionLocal", Session)
    yield Session
    engine.dispose()


@pytest.fixture()
def sm(db):
    from core.session_manager import SessionManager
    return SessionManager()


def _fake_effective_user(request):
    """Per-request fake identity — tests pick the caller with an
    `X-Test-User` header instead of a real cookie/token."""
    return request.headers.get("x-test-user", "alice")


@pytest.fixture()
def client(db, sm, monkeypatch):
    import routes.session_routes as sr_mod
    import routes.side_thread_routes as str_mod

    monkeypatch.setattr(sr_mod, "effective_user", _fake_effective_user)
    monkeypatch.setattr(str_mod, "effective_user", _fake_effective_user)

    app = FastAPI()
    app.include_router(str_mod.setup_side_thread_routes(sm))
    with TestClient(app) as c:
        yield c


def _hdr(user):
    return {"x-test-user": user} if user else {}


def _mk_session(sm, sid, *, owner="alice", name="Main", n_messages=0, endpoint="http://ep", model="m1"):
    sm.create_session(session_id=sid, name=name, endpoint_url=endpoint, model=model, owner=owner)
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        sm.add_message(sid, ChatMessage(role, f"msg-{i}"))
    return sm.get_session(sid)


# ---------------------------------------------------------------------------
# Creating a side thread
# ---------------------------------------------------------------------------

def test_create_side_thread_makes_an_empty_child_and_leaves_parent_untouched(client, sm):
    parent = _mk_session(sm, "p1", n_messages=4)
    parent_history_before = [(m.role, m.content) for m in parent.history]

    r = client.post(
        "/api/session/p1/side-threads",
        json={"anchor_index": 1, "passage": "the interesting bit", "question": "why though?"},
        headers=_hdr("alice"),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"session_id", "wire", "question"}
    assert body["question"] == "why though?"
    new_id = body["session_id"]
    assert new_id != "p1"

    wire = body["wire"]
    assert wire["kind"] == "branch"
    assert wire["source_session_id"] == "p1"
    assert wire["target_session_id"] == new_id
    assert wire["anchor_index"] == 1
    assert wire["anchor_passage"] == "the interesting bit"

    child = sm.get_session(new_id)
    assert child.history == []
    assert child.name.startswith("↳ ")
    assert child.endpoint_url == parent.endpoint_url
    assert child.model == parent.model
    assert child.owner == "alice"

    # The parent is untouched, byte for byte.
    parent_after = sm.get_session("p1")
    assert [(m.role, m.content) for m in parent_after.history] == parent_history_before


def test_create_side_thread_anchor_out_of_range_is_400(client, sm):
    _mk_session(sm, "p1", n_messages=2)
    r = client.post(
        "/api/session/p1/side-threads",
        json={"anchor_index": 5},
        headers=_hdr("alice"),
    )
    assert r.status_code == 400
    assert r.json()["error_class"] == "excursos.anchor_out_of_range"
    assert "error" in r.json()


def test_create_side_thread_on_a_foreign_session_is_404(client, sm):
    _mk_session(sm, "p1", owner="bob", n_messages=2)
    r = client.post(
        "/api/session/p1/side-threads",
        json={"anchor_index": 0},
        headers=_hdr("alice"),
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# inherited_context — the hook
# ---------------------------------------------------------------------------

def test_inherited_context_hooks_the_anchor_slice_and_passage_without_history_index(sm):
    from src.side_threads import create_side_thread, inherited_context

    _mk_session(sm, "p1", n_messages=6)  # msg-0..msg-5
    result = create_side_thread(sm, "alice", "p1", anchor_index=2, passage="quoted passage")
    child_id = result["session_id"]

    inherited = inherited_context(sm, "alice", child_id)

    # anchor_index=2 -> messages 0,1,2 (3 messages) + the passage note.
    assert len(inherited) == 4
    assert [m["content"] for m in inherited[:3]] == ["msg-0", "msg-1", "msg-2"]
    assert inherited[3] == {
        "role": "user",
        "content": '[Regarding this passage: "quoted passage"]',
    }
    # The decisive invariant: none of these carry `_history_index`, so
    # compaction can summarize them but can never delete a row because of
    # them (src/context_compactor.py's HISTORY_INDEX_KEY).
    for msg in inherited:
        assert HISTORY_INDEX_KEY not in msg


def test_inherited_context_is_empty_with_no_wires(sm):
    from src.side_threads import inherited_context

    _mk_session(sm, "lonely", n_messages=2)
    assert inherited_context(sm, "alice", "lonely") == []


def test_inherited_context_missing_anchor_falls_back_to_full_parent_with_note(sm):
    from src.side_threads import create_side_thread, inherited_context

    _mk_session(sm, "p1", n_messages=6)
    result = create_side_thread(sm, "alice", "p1", anchor_index=4)
    child_id = result["session_id"]

    # Truncate the parent below the anchor (as truncate/compact would):
    # drop every DB row after the first 2, and mirror it in-memory.
    parent = sm.get_session("p1")
    parent.history = parent.history[:2]
    from core.database import SessionLocal, ChatMessage as DbChatMessage
    db = SessionLocal()
    try:
        rows = (
            db.query(DbChatMessage)
            .filter(DbChatMessage.session_id == "p1")
            .order_by(DbChatMessage.timestamp)
            .all()
        )
        for row in rows[2:]:
            db.delete(row)
        db.commit()
    finally:
        db.close()

    inherited = inherited_context(sm, "alice", child_id)
    assert [m["content"] for m in inherited[:2]] == ["msg-0", "msg-1"]
    assert "no longer exists in the parent conversation" in inherited[2]["content"]
    assert len(inherited) == 3
    for msg in inherited:
        assert HISTORY_INDEX_KEY not in msg


def test_two_level_side_thread_inherits_the_full_chain_in_order(sm):
    from src.side_threads import create_side_thread, inherited_context

    _mk_session(sm, "root", n_messages=4)  # msg-0..msg-3
    lvl1 = create_side_thread(sm, "alice", "root", anchor_index=1)["session_id"]
    sm.add_message(lvl1, ChatMessage("user", "lvl1-question"))
    sm.add_message(lvl1, ChatMessage("assistant", "lvl1-answer"))

    lvl2 = create_side_thread(sm, "alice", lvl1, anchor_index=1)["session_id"]

    inherited = inherited_context(sm, "alice", lvl2)
    # root[:2] (msg-0, msg-1), then lvl1[:2] (lvl1-question, lvl1-answer) —
    # root-to-leaf order, nothing from lvl2 itself (that's its OWN history).
    assert [m["content"] for m in inherited] == [
        "msg-0", "msg-1", "lvl1-question", "lvl1-answer",
    ]
    for msg in inherited:
        assert HISTORY_INDEX_KEY not in msg


# ---------------------------------------------------------------------------
# References ("traer de vuelta")
# ---------------------------------------------------------------------------

def test_reference_quote_vs_full_block_content(sm):
    from src.side_threads import add_reference, reference_block
    from types import SimpleNamespace

    _mk_session(sm, "main", n_messages=1)
    excurso = _mk_session(sm, "exc", n_messages=0, name="↳ tangent")
    sm.add_message("exc", ChatMessage("user", "first question"))
    sm.add_message("exc", ChatMessage("assistant", "first answer"))
    sm.add_message("exc", ChatMessage("user", "second question"))
    sm.add_message("exc", ChatMessage("assistant", "second answer"))

    quote = add_reference("alice", "exc", "main", depth="quote")
    assert quote["wire"]["depth"] == "quote"
    assert "tokens" in quote

    full_text = reference_block(
        SimpleNamespace(name="tangent", history=sm.get_session("exc").history),
        "full",
        [],
    )
    assert "first question" in full_text and "second question" in full_text
    assert "Trail" not in full_text

    from src.side_threads import _reference_block_for_db
    quote_text = _reference_block_for_db("exc", "tangent", "quote")
    assert quote_text.startswith("[Reference: tangent]")
    # Trail carries the excurso's own opening question (its "chain of one");
    # Q/A below it is only the LATEST exchange, not the whole transcript.
    assert "Trail (upstream questions): first question" in quote_text
    assert "Q: second question" in quote_text
    assert "A: second answer" in quote_text
    assert "first answer" not in quote_text


def test_reference_stale_flips_true_then_false_after_refresh(sm):
    from src.side_threads import add_reference, update_reference, wires_for

    _mk_session(sm, "main", n_messages=1)
    _mk_session(sm, "exc", n_messages=2)

    added = add_reference("alice", "exc", "main")
    wire_id = added["wire"]["id"]

    before = wires_for("alice", "main")
    assert before["references_in"][0]["stale"] is False

    sm.add_message("exc", ChatMessage("user", "grew"))

    grown = wires_for("alice", "main")
    assert grown["references_in"][0]["stale"] is True

    update_reference("alice", wire_id, refresh=True)
    refreshed = wires_for("alice", "main")
    assert refreshed["references_in"][0]["stale"] is False


def test_self_reference_is_400(client, sm):
    _mk_session(sm, "s1", n_messages=1)
    r = client.post("/api/session/s1/references", json={"source_session_id": "s1"}, headers=_hdr("alice"))
    assert r.status_code == 400
    assert r.json()["error_class"] == "excursos.self_reference"


def test_add_reference_is_idempotent(sm):
    from src.side_threads import add_reference

    _mk_session(sm, "main", n_messages=1)
    _mk_session(sm, "exc", n_messages=1)
    first = add_reference("alice", "exc", "main")
    second = add_reference("alice", "exc", "main")
    assert first["wire"]["id"] == second["wire"]["id"]


def test_wiring_again_after_withdraw_revives_the_same_wire(sm):
    """Withdraw archives; wiring the pair again must un-archive THAT row
    (with the depth just asked for and a fresh fingerprint), never leave an
    orphan archived row behind a new one."""
    from core.database import SessionLocal, SessionWire
    from src.side_threads import add_reference, update_reference, wires_for

    _mk_session(sm, "main", n_messages=1)
    _mk_session(sm, "exc", n_messages=1)
    first = add_reference("alice", "exc", "main")["wire"]
    update_reference("alice", first["id"], archived=True)
    assert wires_for("alice", "main")["references_in"] == []

    again = add_reference("alice", "exc", "main", depth="full")["wire"]
    assert again["id"] == first["id"]
    assert again["archived"] is False and again["depth"] == "full"
    db = SessionLocal()
    try:
        rows = db.query(SessionWire).filter(SessionWire.kind == "reference").all()
    finally:
        db.close()
    assert len(rows) == 1


def test_removing_a_reference_does_not_delete_the_side_thread(sm):
    from src.side_threads import add_reference, remove_reference

    _mk_session(sm, "main", n_messages=1)
    _mk_session(sm, "exc", n_messages=2)
    wire_id = add_reference("alice", "exc", "main")["wire"]["id"]

    assert remove_reference("alice", wire_id) is True
    assert sm.get_session("exc") is not None
    assert len(sm.get_session("exc").history) == 2
    # The reference is really gone (DELETE, not archive) — removing it twice
    # reports nothing left to remove.
    assert remove_reference("alice", wire_id) is False


# ---------------------------------------------------------------------------
# The decisive test: the parent never gains messages
# ---------------------------------------------------------------------------

def test_parent_never_gains_messages_from_creating_or_referencing_a_side_thread(sm):
    from src.side_threads import add_reference, create_side_thread

    parent = _mk_session(sm, "p1", n_messages=3)
    before = len(parent.history)

    child_id = create_side_thread(sm, "alice", "p1", anchor_index=1)["session_id"]
    sm.add_message(child_id, ChatMessage("user", "tangent question"))
    add_reference("alice", child_id, "p1")

    after = sm.get_session("p1")
    assert len(after.history) == before


# ---------------------------------------------------------------------------
# Owner scoping across every route
# ---------------------------------------------------------------------------

def test_owner_scoping_is_404_on_every_route(client, sm):
    _mk_session(sm, "mine", owner="alice", n_messages=2)
    _mk_session(sm, "theirs", owner="bob", n_messages=2)

    h = _hdr("alice")
    assert client.post("/api/session/theirs/side-threads", json={"anchor_index": 0}, headers=h).status_code == 404
    assert client.get("/api/session/theirs/side-threads", headers=h).status_code == 404
    assert client.get("/api/session/theirs/thought-map", headers=h).status_code == 404
    assert client.get("/api/session/theirs/context-preview", headers=h).status_code == 404
    assert client.post(
        "/api/session/theirs/references", json={"source_session_id": "mine"}, headers=h
    ).status_code == 404
    # Referencing FROM a foreign session into one's own is also refused.
    assert client.post(
        "/api/session/mine/references", json={"source_session_id": "theirs"}, headers=h
    ).status_code == 404
    assert client.patch(
        "/api/session/theirs/references/nope", json={"refresh": True}, headers=h
    ).status_code == 404
    assert client.delete("/api/session/theirs/references/nope", headers=h).status_code == 404


# ---------------------------------------------------------------------------
# Route response shapes
# ---------------------------------------------------------------------------

def test_wires_for_route_shape(client, sm):
    _mk_session(sm, "p1", n_messages=3)
    child_id = client.post(
        "/api/session/p1/side-threads", json={"anchor_index": 1}, headers=_hdr("alice")
    ).json()["session_id"]

    r = client.get("/api/session/p1/side-threads", headers=_hdr("alice"))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"parent", "children", "references_in", "references_out"}
    assert body["parent"] is None
    assert len(body["children"]) == 1
    assert body["children"][0]["session"]["id"] == child_id
    assert body["children"][0]["reference"] is None
    assert body["children"][0]["stale"] is False

    r2 = client.get(f"/api/session/{child_id}/side-threads", headers=_hdr("alice"))
    child_body = r2.json()
    assert child_body["parent"]["session"]["id"] == "p1"
    assert child_body["parent"]["anchor_state"] == "ok"


def test_thought_map_route_shape(client, sm):
    _mk_session(sm, "root", n_messages=2, name="Root")
    child_id = client.post(
        "/api/session/root/side-threads", json={"anchor_index": 0}, headers=_hdr("alice")
    ).json()["session_id"]

    r = client.get(f"/api/session/{child_id}/thought-map", headers=_hdr("alice"))
    assert r.status_code == 200
    tree = r.json()
    assert tree["id"] == "root"
    assert tree["anchor_index"] is None
    assert len(tree["children"]) == 1
    assert tree["children"][0]["id"] == child_id
    assert tree["children"][0]["current"] is True
    assert tree["children"][0]["anchor_index"] == 0
    assert "truncated" not in tree


def test_context_preview_route_shape(client, sm):
    _mk_session(sm, "p1", n_messages=2)
    child_id = client.post(
        "/api/session/p1/side-threads", json={"anchor_index": 0}, headers=_hdr("alice")
    ).json()["session_id"]

    r = client.get(f"/api/session/{child_id}/context-preview", headers=_hdr("alice"))
    assert r.status_code == 200
    body = r.json()
    assert "total_tokens" in body
    layers = {layer["layer"]: layer for layer in body["layers"]}
    assert set(layers) == {"references", "inherited", "own"}
    assert layers["inherited"]["from"]["session_id"] == "p1"
    assert layers["inherited"]["from"]["anchor_state"] == "ok"
    assert layers["own"]["messages"] == 0


def test_parents_map_route(client, sm):
    _mk_session(sm, "p1", n_messages=1)
    child_id = client.post(
        "/api/session/p1/side-threads", json={"anchor_index": 0}, headers=_hdr("alice")
    ).json()["session_id"]

    r = client.get("/api/side-threads/parents", headers=_hdr("alice"))
    assert r.status_code == 200
    assert r.json() == {"parents": {child_id: "p1"}}


def test_bad_depth_is_400(client, sm):
    _mk_session(sm, "main", n_messages=1)
    _mk_session(sm, "exc", n_messages=1)
    r = client.post(
        "/api/session/main/references",
        json={"source_session_id": "exc", "depth": "essay"},
        headers=_hdr("alice"),
    )
    assert r.status_code == 400
    assert r.json()["error_class"] == "excursos.bad_depth"
