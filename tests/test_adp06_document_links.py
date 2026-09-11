"""Tests for ADP-06 (W1-E): wiki-links/backlinks between Documents.

Covers `src/document_links.py` (parse/resolve/store) and the two routes in
`routes/document_links_routes.py`, plus the save-hook wired into
`routes/document/document_routes.py::update_document`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as db
from src import document_links


# ---------------------------------------------------------------------------
# parse_links — pure
# ---------------------------------------------------------------------------

def test_parse_links_extracts_title_id_and_alias():
    text = "See [[Plan Q1]] and [[doc:abc-123]] and [[Long Title|short]]."
    tokens = document_links.parse_links(text)
    assert [t.target_type for t in tokens] == ["title", "id", "title"]
    assert tokens[0].target_value == "Plan Q1"
    assert tokens[1].target_value == "abc-123"
    assert tokens[2].target_value == "Long Title"
    assert tokens[2].alias == "short"


def test_parse_links_ignores_unclosed_brackets():
    assert document_links.parse_links("this [[ has no closing") == []


def test_parse_links_empty_target_skipped():
    assert document_links.parse_links("[[]]") == []


# ---------------------------------------------------------------------------
# resolve — needs a real SQLAlchemy session (owner scoping is a query filter)
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


def _doc(id, title, owner, content="", version_count=1):
    return db.Document(id=id, title=title, current_content=content, owner=owner,
                        version_count=version_count, is_active=True)


def test_resolve_by_id(sa_session):
    sa_session.add(_doc("d1", "Target", "alice"))
    sa_session.commit()
    token = document_links.parse_links("[[doc:d1]]")[0]
    r = document_links.resolve(sa_session, "alice", token)
    assert r.status == document_links.STATUS_RESOLVED
    assert r.doc_id == "d1"


def test_resolve_by_id_wrong_owner_is_broken_not_leaked(sa_session):
    sa_session.add(_doc("d1", "Target", "bob"))
    sa_session.commit()
    token = document_links.parse_links("[[doc:d1]]")[0]
    r = document_links.resolve(sa_session, "alice", token)
    # Exists, but for a different owner -- must read exactly like "not found",
    # never leak that the id exists for someone else.
    assert r.status == document_links.STATUS_BROKEN


def test_resolve_by_title_unique(sa_session):
    sa_session.add(_doc("d1", "My Plan", "alice"))
    sa_session.commit()
    token = document_links.parse_links("[[My Plan]]")[0]
    r = document_links.resolve(sa_session, "alice", token)
    assert r.status == document_links.STATUS_RESOLVED
    assert r.doc_id == "d1"


def test_resolve_by_title_ambiguous_lists_every_candidate(sa_session):
    sa_session.add(_doc("d1", "Notes", "alice"))
    sa_session.add(_doc("d2", "Notes", "alice"))
    sa_session.commit()
    token = document_links.parse_links("[[Notes]]")[0]
    r = document_links.resolve(sa_session, "alice", token)
    assert r.status == document_links.STATUS_AMBIGUOUS
    assert sorted(r.candidates) == ["d1", "d2"]


def test_resolve_by_title_across_owners_never_merges_candidates(sa_session):
    """Two owners each have one document titled 'Notes' -- from alice's
    side that's a single unambiguous match, not an ambiguous pair."""
    sa_session.add(_doc("d1", "Notes", "alice"))
    sa_session.add(_doc("d2", "Notes", "bob"))
    sa_session.commit()
    token = document_links.parse_links("[[Notes]]")[0]
    r = document_links.resolve(sa_session, "alice", token)
    assert r.status == document_links.STATUS_RESOLVED
    assert r.doc_id == "d1"


def test_resolve_broken_never_guesses_similar_title(sa_session):
    sa_session.add(_doc("d1", "Project Plan", "alice"))
    sa_session.commit()
    token = document_links.parse_links("[[Project Pln]]")[0]  # typo
    r = document_links.resolve(sa_session, "alice", token)
    assert r.status == document_links.STATUS_BROKEN
    assert r.doc_id is None


def test_resolve_rejects_path_traversal(sa_session):
    token = document_links.parse_links("[[../secrets]]")[0]
    r = document_links.resolve(sa_session, "alice", token)
    assert r.status == document_links.STATUS_REJECTED


def test_rename_title_breaks_title_link_but_id_link_survives(sa_session, tmp_path):
    store = document_links.LinkStore(tmp_path / "links.sqlite3")
    target = _doc("d1", "Old Title", "alice")
    sa_session.add(target)
    source = _doc("d2", "Source", "alice", content="[[Old Title]] and [[doc:d1]]")
    sa_session.add(source)
    sa_session.commit()

    document_links.rebuild_for_document(sa_session, source, store=store)
    links = store.links_for("d2")
    by_type = {l["target_type"]: l for l in links}
    assert by_type["title"]["status"] == document_links.STATUS_RESOLVED
    assert by_type["id"]["status"] == document_links.STATUS_RESOLVED

    # Rename the target and rebuild the SOURCE's links again (rebuild is
    # always a recompute against current document rows, never a diff).
    target.title = "New Title"
    sa_session.commit()
    document_links.rebuild_for_document(sa_session, source, store=store)
    links = store.links_for("d2")
    by_type = {l["target_type"]: l for l in links}
    assert by_type["title"]["status"] == document_links.STATUS_BROKEN, \
        "title-based link should break on rename"
    assert by_type["id"]["status"] == document_links.STATUS_RESOLVED, \
        "id-based link must survive a rename"
    assert by_type["id"]["resolved_doc_id"] == "d1"


def test_backlinks_are_owner_scoped_and_never_cross_owner(sa_session, tmp_path):
    store = document_links.LinkStore(tmp_path / "links.sqlite3")
    sa_session.add(_doc("target", "Target", "alice"))
    sa_session.add(_doc("src_alice", "Source A", "alice", content="[[doc:target]]"))
    sa_session.commit()
    document_links.rebuild_for_document(sa_session, sa_session.get(db.Document, "src_alice"), store=store)

    backlinks = store.backlinks_for("target", "alice")
    assert len(backlinks) == 1
    assert backlinks[0]["source_doc_id"] == "src_alice"

    # A different owner asking about the same doc id sees nothing (also
    # covers the case where two owners' docs happen to share an id-looking
    # string -- backlinks are always filtered on the REQUESTER's owner).
    assert store.backlinks_for("target", "bob") == []


def test_rebuild_all_for_owner_only_touches_that_owners_documents(sa_session, tmp_path):
    store = document_links.LinkStore(tmp_path / "links.sqlite3")
    sa_session.add(_doc("a1", "A1", "alice", content="[[doc:a2]]"))
    sa_session.add(_doc("a2", "A2", "alice"))
    sa_session.add(_doc("b1", "B1", "bob", content="[[doc:a2]]"))  # bob can't see alice's a2
    sa_session.commit()
    count = document_links.rebuild_all_for_owner(sa_session, "alice", store=store)
    assert count == 2
    bob_links = store.links_for("b1")
    assert bob_links == [], "rebuild_all_for_owner(alice) must not touch bob's documents"


# ---------------------------------------------------------------------------
# Routes (TestClient, same pattern as tests/test_document_editor_conflict.py)
# ---------------------------------------------------------------------------

def _build_client(tmp_path, monkeypatch, user="alice"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    engine = create_engine("sqlite:///" + str(tmp_path / "routes.db"),
                            connect_args={"check_same_thread": False})
    db.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    from routes.document import document_routes as droutes
    from routes import document_links_routes as lroutes
    monkeypatch.setattr(droutes, "SessionLocal", Session)
    monkeypatch.setattr(lroutes, "SessionLocal", Session)
    monkeypatch.setattr(droutes, "get_current_user", lambda request: user)
    monkeypatch.setattr(lroutes, "get_current_user", lambda request: user)

    store = document_links.LinkStore(tmp_path / "links.sqlite3")
    monkeypatch.setattr(document_links, "_store", store)

    from unittest.mock import MagicMock
    app = FastAPI()
    app.include_router(droutes.setup_document_routes(MagicMock()))
    app.include_router(lroutes.setup_document_links_routes())
    client = TestClient(app)
    return client, Session


def test_save_hook_updates_link_index_and_backlinks_route(tmp_path, monkeypatch):
    client, Session = _build_client(tmp_path, monkeypatch)
    with Session() as s:
        s.add(_doc("target", "Target Doc", "alice"))
        s.add(_doc("src", "Source Doc", "alice", content="start"))
        s.commit()

    resp = client.put("/api/document/src", json={"content": "refers to [[Target Doc]]"})
    assert resp.status_code == 200, resp.text

    links_resp = client.get("/api/documents/src/links")
    assert links_resp.status_code == 200
    links = links_resp.json()["links"]
    assert len(links) == 1
    assert links[0]["status"] == "resolved"
    assert links[0]["resolved_doc_id"] == "target"

    back_resp = client.get("/api/documents/target/backlinks")
    assert back_resp.status_code == 200
    backlinks = back_resp.json()["backlinks"]
    assert len(backlinks) == 1
    assert backlinks[0]["source_doc_id"] == "src"


def test_rebuild_route_is_owner_scoped(tmp_path, monkeypatch):
    client, Session = _build_client(tmp_path, monkeypatch, user="alice")
    with Session() as s:
        s.add(_doc("a1", "A1", "alice", content="[[doc:a2]]"))
        s.add(_doc("a2", "A2", "alice"))
        s.commit()
    resp = client.post("/api/documents/links/rebuild")
    assert resp.status_code == 200, resp.text
    assert resp.json()["documents_processed"] == 2
