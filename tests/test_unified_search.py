"""Tests for src.unified_search: mixing/ranking, per-source caps, error and
timeout isolation, and that each real source adapter maps fields correctly
onto the underlying (monkeypatched) internal function it calls.
"""

from __future__ import annotations

import tempfile
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from src import unified_search


@pytest.fixture
def db_factory(monkeypatch):
    """A throwaway sqlite DB, wired in place of `core.database.SessionLocal`
    — same pattern as tests/test_document_tidy_null_timestamp.py. The
    source adapters do `from core.database import SessionLocal` *inside*
    their function body, so they pick up this patched value at call time.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    engine = create_engine(
        f"sqlite:///{tmp.name}", connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    TS = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(cdb, "SessionLocal", TS)
    return TS


# ---------------------------------------------------------------------------
# Core mixing / ranking behaviour (source functions monkeypatched directly)
# ---------------------------------------------------------------------------

def _row(source, id_, title, score=0.0, snippet="s", url="/x", when=None):
    return {
        "type": source, "id": id_, "title": title, "snippet": snippet,
        "url": url, "when": when, "score": score,
    }


def test_empty_query_returns_immediately_without_calling_sources(monkeypatch):
    called = []

    def _boom(owner, q, limit):
        called.append(True)
        raise AssertionError("should not be called for an empty query")

    for name in unified_search.SOURCES:
        monkeypatch.setattr(unified_search, f"_{name}", _boom)
        monkeypatch.setitem(unified_search._SOURCE_FUNCS, name, _boom)

    result = unified_search.search("owner", "   ")
    assert result["results"] == []
    assert result["counts"] == {}
    assert result["errors"] == {}
    assert not called
    assert isinstance(result["elapsed_ms"], int)


def test_unknown_types_are_ignored(monkeypatch):
    calls = []

    def fake_chats(owner, q, limit):
        calls.append("chats")
        return [_row("chats", "1", "hit")]

    monkeypatch.setitem(unified_search._SOURCE_FUNCS, "chats", fake_chats)

    # Only unknown types requested -> nothing runs.
    result = unified_search.search("owner", "hello", types=["bogus", "also-bogus"])
    assert result["results"] == []
    assert calls == []

    # A mix of known + unknown -> only the known one runs.
    result = unified_search.search("owner", "hello", types=["bogus", "chats"])
    assert calls == ["chats"]
    assert len(result["results"]) == 1
    assert result["results"][0]["type"] == "chats"


def test_mixing_ranks_by_reciprocal_rank_with_title_boost(monkeypatch):
    def fake_a(owner, q, limit):
        # rank 0 -> 1/1 = 1.0, no title match
        return [_row("chats", "a1", "unrelated title")]

    def fake_b(owner, q, limit):
        # rank 0 -> 1/1 = 1.0 + 0.5 title boost ("widget" in title) = 1.5
        # rank 1 -> 1/2 = 0.5
        return [
            _row("brain", "b1", "a Widget note"),
            _row("brain", "b2", "second"),
        ]

    monkeypatch.setitem(unified_search._SOURCE_FUNCS, "chats", fake_a)
    monkeypatch.setitem(unified_search._SOURCE_FUNCS, "brain", fake_b)

    result = unified_search.search(
        "owner", "widget", types=["chats", "brain"], limit=10)
    ids_in_order = [r["id"] for r in result["results"]]
    assert ids_in_order == ["b1", "a1", "b2"]
    assert result["results"][0]["score"] == pytest.approx(1.5)
    assert result["results"][1]["score"] == pytest.approx(1.0)
    assert result["results"][2]["score"] == pytest.approx(0.5)
    assert result["counts"] == {"chats": 1, "brain": 2}


def test_equal_scores_are_stable_by_source_then_rank(monkeypatch):
    def fake_a(owner, q, limit):
        return [_row("chats", "a1", "no match here")]

    def fake_b(owner, q, limit):
        return [_row("brain", "b1", "no match either")]

    monkeypatch.setitem(unified_search._SOURCE_FUNCS, "chats", fake_a)
    monkeypatch.setitem(unified_search._SOURCE_FUNCS, "brain", fake_b)

    result = unified_search.search("owner", "zzz", types=["chats", "brain"])
    # Both rank 0 -> score 1.0 each; dict iteration order over `per_source`
    # follows submission order == `wanted` order == ["chats", "brain"].
    assert [r["id"] for r in result["results"]] == ["a1", "b1"]


def test_per_source_cap(monkeypatch):
    def fake_many(owner, q, limit):
        # Deliberately return more than `limit` — the caller must still cap.
        return [_row("chats", str(i), f"title {i}") for i in range(20)]

    monkeypatch.setitem(unified_search._SOURCE_FUNCS, "chats", fake_many)

    result = unified_search.search("owner", "q", types=["chats"], limit=3)
    assert result["counts"]["chats"] == 3
    assert len(result["results"]) == 3


def test_failing_source_goes_to_errors_without_breaking_others(monkeypatch):
    def fake_broken(owner, q, limit):
        raise RuntimeError("kaboom")

    def fake_ok(owner, q, limit):
        return [_row("brain", "b1", "fine")]

    monkeypatch.setitem(unified_search._SOURCE_FUNCS, "chats", fake_broken)
    monkeypatch.setitem(unified_search._SOURCE_FUNCS, "brain", fake_ok)

    result = unified_search.search("owner", "q", types=["chats", "brain"])
    assert "chats" in result["errors"]
    assert "kaboom" in result["errors"]["chats"]
    assert result["counts"]["chats"] == 0
    assert len(result["results"]) == 1
    assert result["results"][0]["id"] == "b1"


def test_slow_source_is_cut_by_the_time_budget(monkeypatch):
    def fake_slow(owner, q, limit):
        time.sleep(0.5)
        return [_row("chats", "late", "too slow")]

    def fake_fast(owner, q, limit):
        return [_row("brain", "quick", "fast")]

    monkeypatch.setitem(unified_search._SOURCE_FUNCS, "chats", fake_slow)
    monkeypatch.setitem(unified_search._SOURCE_FUNCS, "brain", fake_fast)

    started = time.monotonic()
    result = unified_search.search(
        "owner", "q", types=["chats", "brain"], time_budget=0.05)
    elapsed = time.monotonic() - started

    assert "chats" in result["errors"]
    assert "chats" not in result["counts"] or result["counts"]["chats"] == 0
    assert len(result["results"]) == 1
    assert result["results"][0]["id"] == "quick"
    # We must not have waited for the full 0.5s sleep to observe the timeout.
    assert elapsed < 0.5


def test_url_formats(monkeypatch):
    def fake_chats(owner, q, limit):
        return [unified_search._result(
            source="chats", id_="m1", title="t", snippet="s",
            url="/studio?s=sess1&m=m1", when=None,
        )]

    def fake_board(owner, q, limit):
        return [unified_search._result(
            source="board", id_="FAU-1", title="t", snippet="s",
            url="/projects/proj1?tab=board&issue=FAU-1", when=None,
        )]

    monkeypatch.setitem(unified_search._SOURCE_FUNCS, "chats", fake_chats)
    monkeypatch.setitem(unified_search._SOURCE_FUNCS, "board", fake_board)

    result = unified_search.search("owner", "q", types=["chats", "board"])
    urls = {r["type"]: r["url"] for r in result["results"]}
    assert urls["chats"] == "/studio?s=sess1&m=m1"
    assert urls["board"] == "/projects/proj1?tab=board&issue=FAU-1"


def test_snippet_is_collapsed_and_capped():
    long_text = ("word " * 200) + "\n\n\ttabbed   spaced"
    row = unified_search._result(
        source="notes", id_="1", title="t", snippet=long_text,
        url="/notes?n=1", when=None,
    )
    assert len(row["snippet"]) <= 240
    assert "\n" not in row["snippet"]
    assert "\t" not in row["snippet"]
    assert "  " not in row["snippet"]


# ---------------------------------------------------------------------------
# Real adapters — the underlying module function is monkeypatched, proving
# the adapter maps fields correctly.
# ---------------------------------------------------------------------------

def test_chats_adapter_maps_session_search_result(monkeypatch):
    import src.session_search as session_search

    class FakeHit:
        def to_dict(self):
            return {
                "message_id": "msg-1",
                "session_id": "sess-1",
                "session_name": "My Chat",
                "role": "user",
                "content_snippet": "hello   world",
                "timestamp": "2026-01-01T00:00:00",
                "context_before": [],
                "context_after": [],
            }

    captured = {}

    def fake_search_session_messages(q, limit=20, owner=None, restrict_owner=True,
                                      include_legacy_owner=True):
        captured.update(q=q, limit=limit, owner=owner, restrict_owner=restrict_owner)
        return [FakeHit()]

    monkeypatch.setattr(session_search, "search_session_messages", fake_search_session_messages)
    monkeypatch.setattr(
        "src.unified_search.SOURCES", unified_search.SOURCES)  # no-op, keeps import path warm

    rows = unified_search._chats("owner1", "hello", 5)
    assert captured["owner"] == "owner1"
    assert captured["restrict_owner"] is True
    assert len(rows) == 1
    row = rows[0]
    assert row["type"] == "chats"
    assert row["id"] == "msg-1"
    assert row["title"] == "My Chat"
    assert row["url"] == "/studio?s=sess-1&m=msg-1"
    assert row["when"] == "2026-01-01T00:00:00"
    assert "hello" in row["snippet"]


def test_chats_adapter_omits_m_when_message_id_missing(monkeypatch):
    import src.session_search as session_search

    class FakeHit:
        def to_dict(self):
            return {
                "message_id": "",
                "session_id": "sess-2",
                "session_name": "Other",
                "content_snippet": "x",
                "timestamp": None,
            }

    monkeypatch.setattr(
        session_search, "search_session_messages",
        lambda *a, **k: [FakeHit()],
    )
    rows = unified_search._chats(None, "x", 5)
    assert rows[0]["url"] == "/studio?s=sess-2"


def test_brain_adapter_maps_notes_search(monkeypatch):
    from src.brain import notes as brain_notes

    captured = {}

    def fake_search(owner, q, *, limit=20):
        captured.update(owner=owner, q=q, limit=limit)
        return [{"path": "Folder/Note.md", "title": "A Note", "kind": "note",
                 "snippet": "a snippet", "score": 1.0}]

    monkeypatch.setattr(brain_notes, "search", fake_search)
    rows = unified_search._brain("owner1", "term", 7)
    assert captured == {"owner": "owner1", "q": "term", "limit": 7}
    assert rows[0]["type"] == "brain"
    assert rows[0]["id"] == "Folder/Note.md"
    assert rows[0]["title"] == "A Note"
    assert rows[0]["url"] == "/brain?note=Folder%2FNote.md"


def test_skills_adapter_maps_get_relevant_skills(monkeypatch):
    import services.memory.skills as skills_module

    class FakeSkillsManager:
        def __init__(self, data_dir):
            pass

        def load(self, owner=None):
            return [{"name": "email-triage", "description": "Sorts email"}]

        def get_relevant_skills(self, query, skills, max_items=5):
            assert query == "email"
            return skills

    monkeypatch.setattr(skills_module, "SkillsManager", FakeSkillsManager)

    rows = unified_search._skills("owner1", "email", 5)
    assert rows[0]["type"] == "skills"
    assert rows[0]["id"] == "email-triage"
    assert rows[0]["title"] == "email-triage"
    assert rows[0]["snippet"] == "Sorts email"
    assert rows[0]["url"] == "/skills?skill=email-triage"


def test_board_adapter_maps_project_board_list_issues(monkeypatch):
    from services import projects as projects_module
    from src import project_board

    class FakeStore:
        def list(self, owner=None):
            return [{"id": "proj-1", "name": "Proj"}]

    monkeypatch.setattr(projects_module, "get_store", lambda: FakeStore())

    def fake_list_issues(project_id, *, q="", limit=50, **kwargs):
        assert project_id == "proj-1"
        return ([{"id": "FAU-1", "title": "Fix bug", "updated_at": "2026-02-02T00:00:00"}], None)

    monkeypatch.setattr(project_board, "list_issues", fake_list_issues)

    rows = unified_search._board("owner1", "bug", 10)
    assert rows[0]["type"] == "board"
    assert rows[0]["id"] == "FAU-1"
    assert rows[0]["title"] == "Fix bug"
    assert rows[0]["url"] == "/projects/proj-1?tab=board&issue=FAU-1"
    assert rows[0]["when"] == "2026-02-02T00:00:00"


def test_notes_adapter_filters_and_maps_owner_notes(db_factory):
    from core.database import Note

    db = db_factory()
    try:
        db.add(Note(id="n1", owner="alice", title="Grocery list",
                     content="milk, eggs, bread", archived=False))
        db.add(Note(id="n2", owner="alice", title="Unrelated",
                     content="nothing matching", archived=False))
        db.add(Note(id="n3", owner="bob", title="Grocery run",
                     content="also milk", archived=False))
        db.add(Note(id="n4", owner="alice", title="Old grocery note",
                     content="milk too", archived=True))
        db.commit()
    finally:
        db.close()

    rows = unified_search._notes("alice", "milk", 10)
    ids = {r["id"] for r in rows}
    assert ids == {"n1"}  # not bob's (owner), not archived (n4), not n2 (no match)
    assert rows[0]["type"] == "notes"
    assert rows[0]["url"] == "/notes?n=n1"
    assert rows[0]["title"] == "Grocery list"


def test_documents_adapter_filters_and_maps_owner_documents(db_factory):
    from core.database import Document

    db = db_factory()
    try:
        db.add(Document(id="d1", owner="alice", title="Budget plan",
                         current_content="numbers about the budget",
                         is_active=True, archived=False))
        db.add(Document(id="d2", owner="alice", title="Other",
                         current_content="nothing relevant",
                         is_active=True, archived=False))
        db.add(Document(id="d3", owner="bob", title="Budget too",
                         current_content="budget stuff",
                         is_active=True, archived=False))
        db.add(Document(id="d4", owner="alice", title="Archived budget",
                         current_content="budget archived",
                         is_active=True, archived=True))
        db.commit()
    finally:
        db.close()

    rows = unified_search._documents("alice", "budget", 10)
    ids = {r["id"] for r in rows}
    assert ids == {"d1"}
    assert rows[0]["type"] == "documents"
    assert rows[0]["url"] == "/studio?doc=d1"
    assert rows[0]["title"] == "Budget plan"


def test_gallery_adapter_filters_and_maps_owner_images(db_factory):
    from core.database import GalleryImage

    db = db_factory()
    try:
        db.add(GalleryImage(id="g1", owner="alice", filename="g1.png",
                             prompt="a red dragon", is_active=True))
        db.add(GalleryImage(id="g2", owner="alice", filename="g2.png",
                             prompt="a blue whale", is_active=True))
        db.add(GalleryImage(id="g3", owner="bob", filename="g3.png",
                             prompt="a red truck", is_active=True))
        db.add(GalleryImage(id="g4", owner="alice", filename="g4.png",
                             prompt="an old red dragon", is_active=False))
        db.commit()
    finally:
        db.close()

    rows = unified_search._gallery("alice", "dragon", 10)
    ids = {r["id"] for r in rows}
    assert ids == {"g1"}
    assert rows[0]["type"] == "gallery"
    assert rows[0]["url"] == "/library?type=imagen&img=g1"
    assert rows[0]["title"] == "a red dragon"


def test_board_adapter_skips_a_project_whose_issues_lookup_fails(monkeypatch):
    from services import projects as projects_module
    from src import project_board

    class FakeStore:
        def list(self, owner=None):
            return [{"id": "bad-project"}, {"id": "good-project"}]

    monkeypatch.setattr(projects_module, "get_store", lambda: FakeStore())

    def fake_list_issues(project_id, *, q="", limit=50, **kwargs):
        if project_id == "bad-project":
            raise RuntimeError("db locked")
        return ([{"id": "OK-1", "title": "Works", "updated_at": None}], None)

    monkeypatch.setattr(project_board, "list_issues", fake_list_issues)

    rows = unified_search._board("owner1", "q", 10)
    assert len(rows) == 1
    assert rows[0]["id"] == "OK-1"
