"""Tests for ADP-05 (W1-E): comments anchored to a quote+context inside a
Document, and accept-at-anchor proposals.

Covers `src/document_comments.py` (locate/relocate/accept) and the routes
in `routes/document_comments_routes.py`, plus the relocate hook wired into
`routes/document/document_routes.py::update_document`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as db
from src import document_comments


# ---------------------------------------------------------------------------
# locate_quote / block indexing — pure
# ---------------------------------------------------------------------------

def test_locate_unique_quote():
    r = document_comments.locate_quote("hello world", "world")
    assert r.status == "found"
    assert r.start == 6 and r.end == 11


def test_locate_missing_quote():
    r = document_comments.locate_quote("hello world", "goodbye")
    assert r.status == "not_found"


def test_locate_repeated_quote_without_disambiguating_context_is_ambiguous():
    text = "cat sat. cat ran."
    r = document_comments.locate_quote(text, "cat")
    assert r.status == "ambiguous"


def test_locate_repeated_quote_with_context_disambiguates():
    text = "cat sat. cat ran."
    # Second "cat" is preceded by ". " and followed by " ran" -- unique.
    before, after = text[7:9], text[12:16]
    r = document_comments.locate_quote(text, "cat", before, after)
    assert r.status == "found"
    assert r.start == 9


def test_block_index_for_offset_counts_blank_line_blocks():
    text = "para one\n\npara two\n\npara three"
    assert document_comments.block_index_for_offset(text, 2) == 0
    assert document_comments.block_index_for_offset(text, 12) == 1
    assert document_comments.block_index_for_offset(text, 25) == 2


# ---------------------------------------------------------------------------
# relocate — acceptance criteria straight from the ficha
# ---------------------------------------------------------------------------

class _FakeComment:
    def __init__(self, quote, before_ctx="", after_ctx="", state="open"):
        self.quote = quote
        self.before_ctx = before_ctx
        self.after_ctx = after_ctx
        self.state = state


def test_inserting_text_before_comment_preserves_its_anchor():
    original = "Intro.\n\nThe budget is $500."
    c = _FakeComment("The budget is $500.")
    r0 = document_comments.locate_quote(original, c.quote)
    assert r0.status == "found"

    edited = "Intro.\n\nA new paragraph appears first.\n\nThe budget is $500."
    upd = document_comments.relocate(c, edited)
    assert upd["state"] == "open"
    assert upd["structural_pos"] is not None
    assert edited[upd["_start"]:upd["_end"]] == c.quote


def test_repeated_text_does_not_relocate_arbitrarily():
    c = _FakeComment("the total is $10")
    edited = "Section A: the total is $10. Section B: the total is $10."
    upd = document_comments.relocate(c, edited)
    assert upd["state"] == "orphan"
    assert upd["structural_pos"] is None


def test_relocate_can_heal_an_orphan_once_unambiguous_again():
    c = _FakeComment("the total is $10", state="orphan")
    edited = "Section A: the total is $10 exactly."  # now unique
    upd = document_comments.relocate(c, edited)
    assert upd["state"] == "open"


def test_relocate_never_touches_resolved_comments_state():
    c = _FakeComment("the total is $10", state="resolved")
    upd = document_comments.relocate(c, "the total is $10")
    assert upd["state"] == "resolved"


# ---------------------------------------------------------------------------
# new_comment / accept — needs a real Document row
# ---------------------------------------------------------------------------

@pytest.fixture()
def sa_session(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "docs.db"),
                            connect_args={"check_same_thread": False})
    db.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def _doc(session, content, owner="alice", version_count=1):
    d = db.Document(id="doc1", title="Doc", current_content=content, owner=owner,
                     version_count=version_count, is_active=True)
    session.add(d)
    session.commit()
    return d


def test_new_comment_anchors_against_current_content(sa_session):
    doc = _doc(sa_session, "The budget is $500 this quarter.")
    c = document_comments.new_comment(doc, quote="$500", body="check this figure")
    sa_session.add(c)
    sa_session.commit()
    assert c.state == "open"
    assert c.base_version == 1
    assert c.quote == "$500"


def test_new_comment_rejects_missing_quote(sa_session):
    doc = _doc(sa_session, "no numbers here")
    with pytest.raises(document_comments.CommentError) as ei:
        document_comments.new_comment(doc, quote="$500", body="x")
    assert ei.value.error_class == "document_comments.quote_not_found"


def test_new_comment_rejects_ambiguous_quote_without_context(sa_session):
    doc = _doc(sa_session, "cat sat. cat ran.")
    with pytest.raises(document_comments.CommentError) as ei:
        document_comments.new_comment(doc, quote="cat", body="x")
    assert ei.value.error_class == "document_comments.quote_ambiguous"


def test_accept_replaces_only_at_the_anchor_not_first_global_occurrence(sa_session):
    doc = _doc(sa_session, "Section A: the fee is old. Section B: the fee is old.")
    # Anchor the SECOND occurrence explicitly via context.
    quote = "the fee is old"
    before = "Section B: "
    after = "."
    c = document_comments.new_comment(doc, quote=quote, body="fix B",
                                       before_ctx=before, after_ctx=after,
                                       proposal_find="old", proposal_replace="new")
    sa_session.add(c)
    sa_session.commit()

    new_content = document_comments.accept(sa_session, doc, c)
    sa_session.commit()

    assert new_content == "Section A: the fee is old. Section B: the fee is new."
    assert c.state == "resolved"
    assert doc.version_count == 2


def test_accept_fails_closed_when_quote_edited_by_human(sa_session):
    doc = _doc(sa_session, "The fee is old.")
    c = document_comments.new_comment(doc, quote="The fee is old.", body="update",
                                       proposal_find="old", proposal_replace="new")
    sa_session.add(c)
    sa_session.commit()

    # Human edits the quoted sentence itself before the proposal is accepted.
    doc.current_content = "The fee is TBD."
    sa_session.commit()

    with pytest.raises(document_comments.BaseChangedError) as ei:
        document_comments.accept(sa_session, doc, c)
    assert ei.value.error_class == "document_comments.base_changed"
    assert doc.current_content == "The fee is TBD.", "a failed accept must not touch content"


def test_accept_preserves_unrelated_human_edit_made_after_the_comment(sa_session):
    doc = _doc(sa_session, "Intro.\n\nThe fee is old.")
    c = document_comments.new_comment(doc, quote="The fee is old.", body="update",
                                       proposal_find="old", proposal_replace="new")
    sa_session.add(c)
    sa_session.commit()

    # Human edits an UNRELATED paragraph -- the anchor text itself is untouched.
    doc.current_content = "Intro, now longer.\n\nThe fee is old."
    sa_session.commit()

    new_content = document_comments.accept(sa_session, doc, c)
    assert new_content == "Intro, now longer.\n\nThe fee is new."
    assert "Intro, now longer." in new_content, "the human's unrelated edit must survive the accept"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _build_client(tmp_path, monkeypatch, user="alice"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from unittest.mock import MagicMock
    engine = create_engine("sqlite:///" + str(tmp_path / "routes.db"),
                            connect_args={"check_same_thread": False})
    db.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    from routes.document import document_routes as droutes
    from routes import document_comments_routes as croutes
    monkeypatch.setattr(droutes, "SessionLocal", Session)
    monkeypatch.setattr(croutes, "SessionLocal", Session)
    monkeypatch.setattr(droutes, "get_current_user", lambda request: user)
    monkeypatch.setattr(croutes, "get_current_user", lambda request: user)

    app = FastAPI()
    app.include_router(droutes.setup_document_routes(MagicMock()))
    app.include_router(croutes.setup_document_comments_routes())
    return TestClient(app), Session


def test_comment_crud_and_accept_over_http(tmp_path, monkeypatch):
    client, Session = _build_client(tmp_path, monkeypatch)
    with Session() as s:
        s.add(db.Document(id="doc1", title="Doc", current_content="The fee is old.",
                           owner="alice", version_count=1, is_active=True))
        s.commit()

    create = client.post("/api/documents/doc1/comments",
                          json={"quote": "old", "body": "typo?",
                                "proposal": {"find": "old", "replace": "new"}})
    assert create.status_code == 201, create.text
    comment_id = create.json()["comment"]["id"]

    listing = client.get("/api/documents/doc1/comments")
    assert len(listing.json()["comments"]) == 1

    accept = client.post(f"/api/documents/doc1/comments/{comment_id}/accept")
    assert accept.status_code == 200, accept.text
    assert accept.json()["document"]["current_content"] == "The fee is new."
    assert accept.json()["comment"]["state"] == "resolved"


def test_accept_over_http_conflicts_when_document_changed(tmp_path, monkeypatch):
    client, Session = _build_client(tmp_path, monkeypatch)
    with Session() as s:
        s.add(db.Document(id="doc1", title="Doc", current_content="The fee is old.",
                           owner="alice", version_count=1, is_active=True))
        s.commit()
    create = client.post("/api/documents/doc1/comments",
                          json={"quote": "The fee is old.", "body": "typo?",
                                "proposal": {"find": "old", "replace": "new"}})
    comment_id = create.json()["comment"]["id"]

    # A concurrent PUT changes the anchored sentence entirely.
    put = client.put("/api/document/doc1", json={"content": "The fee is TBD."})
    assert put.status_code == 200, put.text

    accept = client.post(f"/api/documents/doc1/comments/{comment_id}/accept")
    assert accept.status_code == 409
    assert accept.json()["error_class"] == "document_comments.base_changed"


def test_save_hook_orphans_comment_when_quote_disappears(tmp_path, monkeypatch):
    client, Session = _build_client(tmp_path, monkeypatch)
    with Session() as s:
        s.add(db.Document(id="doc1", title="Doc", current_content="The fee is old.",
                           owner="alice", version_count=1, is_active=True))
        s.commit()
    create = client.post("/api/documents/doc1/comments", json={"quote": "old", "body": "x"})
    comment_id = create.json()["comment"]["id"]

    resp = client.put("/api/document/doc1", json={"content": "completely rewritten"})
    assert resp.status_code == 200, resp.text

    listing = client.get("/api/documents/doc1/comments")
    comment = [c for c in listing.json()["comments"] if c["id"] == comment_id][0]
    assert comment["state"] == "orphan"
