"""Transcript search bounded in time: `after` and the "as of" view `before`,
on both the FTS and the LIKE path, plus the agent tool's argument lines."""
from datetime import datetime, timedelta

import pytest

from src.session_search import parse_when, search_session_messages
from src.tool_execution import _search_time_bounds
from tests.test_session_search import _add_message, _add_session, _db


def _seed(db):
    base = datetime(2026, 9, 1, 10, 0, 0)
    _add_session(db, "s1", owner="alice", name="Hangar decisions", folder="Hangar")
    _add_session(db, "s2", owner="alice", name="Other project", folder="Other")
    _add_message(db, "s1", "m1", "user", "we chose the sqlite ledger for the hangar", base)
    _add_message(db, "s1", "m2", "user", "we chose postgres for the hangar instead", base + timedelta(days=30))
    _add_message(db, "s2", "m3", "user", "we chose redis for the hangar cache", base + timedelta(days=40))
    db.commit()


@pytest.mark.parametrize("with_fts", [True, False])
def test_before_is_an_as_of_view_and_after_a_window(with_fts):
    db = _db(with_fts=with_fts)
    try:
        _seed(db)
        everything = {r.message_id for r in search_session_messages("hangar", owner="alice", db=db)}
        assert everything == {"m1", "m2", "m3"}
        as_of = {r.message_id for r in search_session_messages("hangar", owner="alice", db=db, before="2026-09-15")}
        assert as_of == {"m1"}
        recent = {r.message_id for r in search_session_messages("hangar", owner="alice", db=db, after="2026-09-20")}
        assert recent == {"m2", "m3"}
        scoped = {r.message_id for r in search_session_messages(
            "hangar", owner="alice", db=db, folder="Hangar", after="2026-09-20")}
        assert scoped == {"m2"}
    finally:
        db.close()


def test_parse_when_accepts_dates_and_rejects_noise():
    assert parse_when("2026-10-01") == datetime(2026, 10, 1)
    assert parse_when("2026-10-01T12:00:00Z") == datetime(2026, 10, 1, 12, 0)
    assert parse_when("yesterday-ish") is None and parse_when("") is None


def test_tool_content_lines_become_time_bounds():
    assert _search_time_bounds("hangar\nafter: 2026-09-20\nbefore: 2026-10-01") == {
        "after": "2026-09-20", "before": "2026-10-01"}
    assert _search_time_bounds("hangar\nas_of: 2026-09-15") == {"before": "2026-09-15"}
    assert _search_time_bounds("just a query") == {}


def _seed_warehouse(db):
    # Three turns of one chat: before, inside and after a requested window.
    _add_session(db, "w1", owner="alice", name="Warehouse planning")
    _add_message(db, "w1", "w-old", "user", "Old state before requested interval", datetime(2026, 8, 1, 9, 0))
    _add_message(db, "w1", "w-hit", "assistant", "Inventory decision: use Warehouse A", datetime(2026, 9, 1, 9, 0))
    _add_message(db, "w1", "w-new", "user", "Future revision: switch to Warehouse B", datetime(2026, 9, 30, 9, 0))
    db.commit()


@pytest.mark.parametrize("with_fts", [True, False])
def test_context_turns_stay_inside_the_time_window(with_fts):
    db = _db(with_fts=with_fts)
    try:
        _seed_warehouse(db)
        windowed = search_session_messages(
            "Inventory decision", owner="alice", db=db, context_messages=3,
            after="2026-08-15", before="2026-09-15")
        assert [r.message_id for r in windowed] == ["w-hit"]
        hit = windowed[0].to_dict()
        assert hit["context_before"] == [] and hit["context_after"] == []
        text_blob = repr(hit)
        assert "Warehouse B" not in text_blob and "Old state" not in text_blob

        # Only `before`: earlier context is in the as-of view, later is not.
        as_of = search_session_messages(
            "Inventory decision", owner="alice", db=db, context_messages=3, before="2026-09-15")[0]
        assert [c["content"] for c in as_of.context_before] == ["Old state before requested interval"]
        assert as_of.context_after == []

        # Only `after`: later context stays, the turn before the window goes.
        since = search_session_messages(
            "Inventory decision", owner="alice", db=db, context_messages=3, after="2026-08-15")[0]
        assert since.context_before == []
        assert [c["content"] for c in since.context_after] == ["Future revision: switch to Warehouse B"]

        # No window: both neighbours, unchanged behaviour.
        free = search_session_messages("Inventory decision", owner="alice", db=db, context_messages=3)[0]
        assert [c["message_id"] for c in free.context_before] == ["w-old"]
        assert [c["message_id"] for c in free.context_after] == ["w-new"]
    finally:
        db.close()


@pytest.mark.parametrize("with_fts", [True, False])
def test_search_chats_tool_text_hides_turns_outside_the_window(with_fts, monkeypatch):
    import asyncio

    from sqlalchemy.orm import sessionmaker

    import src.session_search as session_search
    from src.tools.search import do_search_chats

    db = _db(with_fts=with_fts)
    try:
        _seed_warehouse(db)
        maker = sessionmaker(bind=db.get_bind())
        monkeypatch.setattr(session_search, "SessionLocal", maker)
        import src.owner_identity as owner_identity
        monkeypatch.setattr(owner_identity, "auth_disabled", lambda: False)
        out = asyncio.run(do_search_chats(
            "Inventory decision", owner="alice", after="2026-08-15", before="2026-09-15"))["results"]
        assert "Inventory decision: use Warehouse A" in out
        assert "Warehouse B" not in out and "Old state" not in out
        assert "after 2026-08-15, before 2026-09-15" in out

        unbounded = asyncio.run(do_search_chats("Inventory decision", owner="alice"))["results"]
        assert "Future revision: switch to Warehouse B" in unbounded
    finally:
        db.close()
