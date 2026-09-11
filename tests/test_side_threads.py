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
    # CONTRATO_CABLES2 F2: wires_for gained a "materials" list.
    assert set(body) == {"parent", "children", "references_in", "references_out", "materials"}
    assert body["parent"] is None
    assert body["materials"] == []
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
    # CONTRATO_CABLES2 F2: context_preview gained a "materials" layer, first.
    assert set(layers) == {"materials", "references", "inherited", "own"}
    assert [l["layer"] for l in body["layers"]][0] == "materials"
    assert layers["materials"]["messages"] == 0
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


# ---------------------------------------------------------------------------
# CONTRATO_CABLES2 F1 — replay bajo botón: inherited_context_with_snapshot,
# note_wires_used, wires_for's stale_turns, GET /stale-turns
# ---------------------------------------------------------------------------

def _save_assistant_with_snapshot(sm, session_id, content, snapshot):
    """Stand-in for `routes/chat_helpers.py::save_assistant_response(...,
    wires=snapshot)` — stamps `metadata.side_thread_wires` and advances the
    wires' own `source_fingerprint`, without going through the full chat
    stack (no LLM anywhere in this suite)."""
    from src.side_threads import note_wires_used

    sm.add_message(session_id, ChatMessage("assistant", content, metadata={"side_thread_wires": snapshot}))
    note_wires_used(snapshot)


def test_inherited_context_with_snapshot_and_note_wires_used_reset_stale_turns(sm):
    from src.side_threads import inherited_context_with_snapshot, wires_for

    _mk_session(sm, "main", n_messages=1)
    excurso = _mk_session(sm, "exc", n_messages=0)
    sm.add_message("exc", ChatMessage("user", "q1"))
    sm.add_message("exc", ChatMessage("assistant", "a1"))
    from src.side_threads import add_reference
    wire_id = add_reference("alice", "exc", "main")["wire"]["id"]

    # Turn 1: the reply is saved with the CURRENT snapshot — freshly wired,
    # nothing stale yet.
    messages, snapshot = inherited_context_with_snapshot(sm, "alice", "main")
    assert len(snapshot) == 1
    assert snapshot[0]["wire_id"] == wire_id
    assert snapshot[0]["kind"] == "reference"
    assert snapshot[0]["source_session_id"] == "exc"
    assert snapshot[0]["document_id"] is None
    assert isinstance(snapshot[0]["fingerprint"], str) and snapshot[0]["fingerprint"]
    _save_assistant_with_snapshot(sm, "main", "reply 1", snapshot)

    before = wires_for("alice", "main")
    ref_before = before["references_in"][0]
    assert ref_before["stale"] is False
    assert ref_before["stale_turns"] == {"count": 0, "last_index": None}

    # The excurso grows -> the wire itself goes stale, and turn 1 (the reply
    # that used it) is now flagged as written against the earlier version.
    sm.add_message("exc", ChatMessage("user", "q2"))
    sm.add_message("exc", ChatMessage("assistant", "a2"))

    grown = wires_for("alice", "main")
    ref_grown = grown["references_in"][0]
    assert ref_grown["stale"] is True
    assert ref_grown["stale_turns"]["count"] == 1
    # `last_index` is turn 1's row position in main's OWN history: user(0), assistant(1).
    assert ref_grown["stale_turns"]["last_index"] == 1

    # "Regenerate": truncate the stale reply away (exactly what a real
    # regenerate does — /api/chat/regenerate replaces the last assistant
    # turn, it does not leave the old one sitting in history) and save a NEW
    # reply with a FRESH snapshot -> back to 0.
    main_sess = sm.get_session("main")
    sm.replace_messages("main", main_sess.history[:1])
    messages2, snapshot2 = inherited_context_with_snapshot(sm, "alice", "main")
    assert snapshot2[0]["fingerprint"] != snapshot[0]["fingerprint"]
    _save_assistant_with_snapshot(sm, "main", "reply 2 (regenerated)", snapshot2)

    after = wires_for("alice", "main")
    ref_after = after["references_in"][0]
    assert ref_after["stale"] is False
    assert ref_after["stale_turns"] == {"count": 0, "last_index": None}


def test_note_wires_used_updates_source_fingerprint(sm):
    from src.side_threads import add_reference, note_wires_used

    _mk_session(sm, "main", n_messages=1)
    _mk_session(sm, "exc", n_messages=2)
    wire_id = add_reference("alice", "exc", "main")["wire"]["id"]

    from core.database import SessionLocal, SessionWire
    db = SessionLocal()
    try:
        before = db.query(SessionWire).filter(SessionWire.id == wire_id).first().source_fingerprint
    finally:
        db.close()

    note_wires_used([{"wire_id": wire_id, "fingerprint": "deadbeef1234"}])

    db = SessionLocal()
    try:
        after = db.query(SessionWire).filter(SessionWire.id == wire_id).first().source_fingerprint
    finally:
        db.close()
    assert after == "deadbeef1234"
    assert after != before


def test_stale_turns_route_shape(client, sm):
    from src.side_threads import add_reference

    _mk_session(sm, "main", n_messages=1)
    _mk_session(sm, "exc", n_messages=1, name="Excurso")
    wire = add_reference("alice", "exc", "main")["wire"]
    from src.side_threads import inherited_context_with_snapshot
    _messages, snapshot = inherited_context_with_snapshot(sm, "alice", "main")
    _save_assistant_with_snapshot(sm, "main", "reply", snapshot)

    r = client.get("/api/session/main/stale-turns", headers=_hdr("alice"))
    assert r.status_code == 200
    assert r.json() == {"turns": {}}

    sm.add_message("exc", ChatMessage("user", "grew"))
    r2 = client.get("/api/session/main/stale-turns", headers=_hdr("alice"))
    body = r2.json()
    assert set(body) == {"turns"}
    assert list(body["turns"].keys()) == ["1"]  # main: user(0), assistant(1)
    assert body["turns"]["1"] == [{"wire_id": wire["id"], "label": "Excurso"}]


def test_stale_turns_owner_scoping_is_404(client, sm):
    _mk_session(sm, "theirs", owner="bob", n_messages=1)
    r = client.get("/api/session/theirs/stale-turns", headers=_hdr("alice"))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# CONTRATO_CABLES2 F2 — materiales cableados: add/update/remove_material,
# inherited_context ordering, wires_for materials
# ---------------------------------------------------------------------------

def _mk_document(doc_id, *, owner="alice", title="Doc", content="hello world"):
    from core.database import SessionLocal, Document
    db = SessionLocal()
    try:
        doc = Document(id=doc_id, title=title, current_content=content, version_count=1, owner=owner)
        db.add(doc)
        db.commit()
    finally:
        db.close()
    return doc_id


def test_add_material_document_not_owned_is_404(sm):
    from src.side_threads import add_material, SideThreadError

    _mk_session(sm, "main", n_messages=1)
    _mk_document("doc1", owner="bob")

    with pytest.raises(SideThreadError) as excinfo:
        add_material("alice", "main", kind="document", document_id="doc1", quotes=["hi"])
    assert excinfo.value.error_class == "excursos.not_found"
    assert excinfo.value.status == 404


def test_add_material_selection_requires_quotes(sm):
    from src.side_threads import add_material, SideThreadError

    _mk_session(sm, "main", n_messages=1)
    _mk_document("doc1")
    with pytest.raises(SideThreadError) as excinfo:
        add_material("alice", "main", kind="document", document_id="doc1", depth="selection", quotes=[])
    assert excinfo.value.error_class == "excursos.quotes_required"


def test_add_material_selection_vs_full_block_content(sm):
    from src.side_threads import add_material, material_block
    from core.database import SessionLocal, SessionWire

    _mk_session(sm, "main", n_messages=1)
    _mk_document("doc1", content="The quick brown fox jumps over the lazy dog.")

    sel = add_material("alice", "main", kind="document", document_id="doc1", depth="selection", quotes=["quick brown fox"])
    assert "tokens" in sel

    full = add_material("alice", "main", kind="document", document_id="doc1", depth="full")
    assert full["wire"]["id"] != sel["wire"]["id"]

    db = SessionLocal()
    try:
        sel_wire = db.query(SessionWire).filter(SessionWire.id == sel["wire"]["id"]).first()
        full_wire = db.query(SessionWire).filter(SessionWire.id == full["wire"]["id"]).first()
        sel_block = material_block(sel_wire)
        full_block = material_block(full_wire)
    finally:
        db.close()
    assert "quick brown fox" in sel_block["content"]
    assert "1. " in sel_block["content"]
    assert "The quick brown fox jumps over the lazy dog." in full_block["content"]


def test_add_material_document_is_idempotent_on_same_quotes(sm):
    from src.side_threads import add_material

    _mk_session(sm, "main", n_messages=1)
    _mk_document("doc1")
    first = add_material("alice", "main", kind="document", document_id="doc1", quotes=["hello"])
    second = add_material("alice", "main", kind="document", document_id="doc1", quotes=["hello"])
    assert first["wire"]["id"] == second["wire"]["id"]


def test_full_material_reflects_live_edit_and_stale_until_refresh(sm):
    from src.side_threads import add_material, update_material, wires_for
    from core.database import SessionLocal, Document

    _mk_session(sm, "main", n_messages=1)
    _mk_document("doc1", content="version one")
    wire_id = add_material("alice", "main", kind="document", document_id="doc1", depth="full")["wire"]["id"]

    before = wires_for("alice", "main")
    assert before["materials"][0]["stale"] is False

    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == "doc1").first()
        doc.current_content = "version two, edited"
        doc.version_count = 2
        db.commit()
    finally:
        db.close()

    grown = wires_for("alice", "main")
    assert grown["materials"][0]["stale"] is True

    update_material("alice", wire_id, refresh=True)
    refreshed = wires_for("alice", "main")
    assert refreshed["materials"][0]["stale"] is False


def test_note_material_enters_as_note_block_and_never_dedupes(sm):
    from src.side_threads import add_material, inherited_context

    _mk_session(sm, "main", n_messages=1)
    first = add_material("alice", "main", kind="note", note_text="remember this")
    second = add_material("alice", "main", kind="note", note_text="remember this")
    assert first["wire"]["id"] != second["wire"]["id"]

    inherited = inherited_context(sm, "alice", "main")
    note_contents = [m["content"] for m in inherited if m["content"].startswith("[Note]")]
    assert note_contents == ["[Note]\nremember this", "[Note]\nremember this"]


def test_inherited_order_is_materials_then_references_then_branch(sm):
    from src.side_threads import add_material, add_reference, create_side_thread, inherited_context

    parent = _mk_session(sm, "root", n_messages=2)  # msg-0, msg-1
    child_id = create_side_thread(sm, "alice", "root", anchor_index=0)["session_id"]

    _mk_document("doc1", content="doc body")
    add_material("alice", child_id, kind="document", document_id="doc1", depth="full")
    add_material("alice", child_id, kind="note", note_text="a note")

    excurso = _mk_session(sm, "other", n_messages=0)
    sm.add_message("other", ChatMessage("user", "other-q"))
    sm.add_message("other", ChatMessage("assistant", "other-a"))
    add_reference("alice", "other", child_id)

    inherited = inherited_context(sm, "alice", child_id)
    kinds = []
    for m in inherited:
        content = m["content"]
        if content.startswith("wired document context") or "doc body" in content:
            kinds.append("material-doc")
        elif content.startswith("[Note]"):
            kinds.append("material-note")
        elif content.startswith("[Reference:"):
            kinds.append("reference")
        else:
            kinds.append("branch")
    # materials (doc, note) first, then the reference, then the branch chain.
    assert kinds == ["material-doc", "material-note", "reference", "branch"]


def test_remove_material_deletes_the_wire(sm):
    from src.side_threads import add_material, remove_material, wires_for

    _mk_session(sm, "main", n_messages=1)
    wire_id = add_material("alice", "main", kind="note", note_text="temp")["wire"]["id"]
    assert len(wires_for("alice", "main")["materials"]) == 1
    assert remove_material("alice", wire_id) is True
    assert wires_for("alice", "main")["materials"] == []
    assert remove_material("alice", wire_id) is False


def test_materials_routes_shape(client, sm):
    _mk_session(sm, "main", n_messages=1)
    _mk_document("doc1", content="Some document body text here.")

    r = client.post(
        "/api/session/main/materials",
        json={"kind": "document", "document_id": "doc1", "depth": "selection", "quotes": ["document body"]},
        headers=_hdr("alice"),
    )
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"wire", "tokens"}
    wire_id = body["wire"]["id"]
    assert body["wire"]["kind"] == "document"
    assert body["wire"]["quotes"] == ["document body"]

    r2 = client.patch(f"/api/session/main/materials/{wire_id}", json={"archived": True}, headers=_hdr("alice"))
    assert r2.status_code == 200
    assert r2.json()["wire"]["archived"] is True

    r3 = client.delete(f"/api/session/main/materials/{wire_id}", headers=_hdr("alice"))
    assert r3.status_code == 200
    assert r3.json() == {"removed": True}

    r4 = client.delete(f"/api/session/main/materials/{wire_id}", headers=_hdr("alice"))
    assert r4.status_code == 404
    assert r4.json()["error_class"] == "excursos.not_found"


def test_materials_owner_scoping(sm):
    from src.side_threads import add_material, SideThreadError

    _mk_session(sm, "theirs", owner="bob", n_messages=1)
    with pytest.raises(SideThreadError) as excinfo:
        add_material("alice", "theirs", kind="note", note_text="x")
    assert excinfo.value.error_class == "excursos.not_found"


# ---------------------------------------------------------------------------
# CONTRATO_CABLES2 F2 — session_wires migration is idempotent
# ---------------------------------------------------------------------------

def test_session_wire_material_migration_is_idempotent(db):
    """Simulate an install from before F2: a `session_wires` table with the
    CONTRATO_EXCURSOS-era schema (no document_id/note_text/quotes/ranges).
    The migration must add them once, and no-op cleanly on a second run."""
    import core.database as db_mod
    from sqlalchemy import text

    with db_mod.engine.connect() as conn:
        conn.execute(text("DROP TABLE session_wires"))
        conn.execute(text(
            "CREATE TABLE session_wires ("
            "id TEXT PRIMARY KEY, owner TEXT, kind TEXT NOT NULL, "
            "source_session_id TEXT NOT NULL, target_session_id TEXT NOT NULL, "
            "anchor_index INTEGER, anchor_passage TEXT, "
            "depth TEXT NOT NULL DEFAULT 'quote', context_order INTEGER NOT NULL DEFAULT 0, "
            "archived BOOLEAN NOT NULL DEFAULT 0, source_fingerprint TEXT, created_at DATETIME)"
        ))
        conn.commit()
        cols_before = [r[1] for r in conn.execute(text("PRAGMA table_info(session_wires)"))]
    assert "document_id" not in cols_before

    db_mod._migrate_add_session_wire_material_columns()
    db_mod._migrate_add_session_wire_material_columns()  # idempotent, no error

    with db_mod.engine.connect() as conn:
        cols_after = [r[1] for r in conn.execute(text("PRAGMA table_info(session_wires)"))]
    for col in ("document_id", "note_text", "quotes", "ranges"):
        assert col in cols_after
