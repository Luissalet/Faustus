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
