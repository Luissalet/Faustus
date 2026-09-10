"""
tests/test_p1_conn_04_sync_conflicts.py — CONN-04, lote 53.

Acceptance line: "Una nota modificada en el movil no se pisa con el
borrador local antiguo tras volver la red." Before this batch, `push_event`
always overwrote whatever `event_by_uid` returned — a pure last-writer-wins.
This pins the fix at three levels:

  1. `push_event` itself (pure, fake caldav objects — same fixtures
     `tests/test_caldav_writeback.py` already uses) refuses the overwrite
     when the remote's current ETag has moved past what we last saw, and
     returns BOTH versions rather than picking one.
  2. `_persist_writeback_result` (the write-back path's own persistence,
     `src/caldav_writeback.py`, PROPIO) marks the local row `conflict`
     instead of quietly clearing its pending flag as if the push had
     landed.
  3. The two new routes (`routes/calendar_routes.py`, PROPIO) that surface
     and resolve a conflict, called the same direct-endpoint way
     `tests/test_caldav_writeback_route.py` already established for this
     router (its own docstring explains why: TestClient can hang here).

No capability lost (rule 3): `test_push_update_overwrites_existing` in the
existing suite calls `push_event` with an event carrying no `remote_etag` at
all and still expects a plain overwrite — proven still green below.
"""
from __future__ import annotations

import tempfile
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import routes.calendar_routes as croutes
from core.database import CalendarCal, CalendarEvent
from src.caldav_writeback import _persist_writeback_result, push_event

REMOTE_URL = "https://p69-caldav.icloud.com/123/calendars/home/"
from src.caldav_writeback import _stable_cal_id
CAL_ID = _stable_cal_id(REMOTE_URL)


class FakeEvent:
    def __init__(self, url="https://p69-caldav.icloud.com/123/calendars/home/evt-1.ics",
                etag='"server-etag-2"'):
        self.url = url
        self.etag = etag
        self.data = "BEGIN:VCALENDAR\nSUMMARY:Edited on phone\nEND:VCALENDAR"
        self.saved = False
        self.deleted = False

    def save(self):
        self.saved = True

    def delete(self):
        self.deleted = True


class FakeCalendar:
    def __init__(self, url, existing=None):
        self.url = url
        self._existing = existing

    def event_by_uid(self, uid):
        if self._existing is None:
            raise Exception("not found")
        return self._existing


def _ev(**over):
    base = dict(
        uid="evt-1", summary="Dentist (local draft)", description="",
        location="", dtstart=datetime(2026, 6, 10, 14, 0),
        dtend=datetime(2026, 6, 10, 15, 0), all_day=False, is_utc=True, rrule="",
    )
    base.update(over)
    return base


# ── 1. push_event: explicit conflict, both versions, no overwrite ─────────

def test_a_stale_local_draft_does_not_overwrite_a_remote_change():
    existing = FakeEvent(etag='"server-etag-2"')          # what the phone left behind
    cal = FakeCalendar(REMOTE_URL, existing=existing)
    local = _ev(summary="Old local draft", remote_etag='"server-etag-1"')  # what we last synced

    res = push_event([cal], CAL_ID, local, delete=False)

    assert res["ok"] is False and res["conflict"] is True
    assert existing.saved is False, "the phone's edit must not be overwritten"
    assert res["local"]["summary"] == "Old local draft"
    assert "Edited on phone" in res["remote_snapshot"]
    assert res["remote_etag"] == '"server-etag-2"'


def test_matching_etags_proceed_as_a_normal_push():
    existing = FakeEvent(etag='"server-etag-1"')
    cal = FakeCalendar(REMOTE_URL, existing=existing)
    local = _ev(summary="Moved", remote_etag='"server-etag-1"')

    res = push_event([cal], CAL_ID, local, delete=False)
    assert res["ok"] is True and res.get("updated") is True
    assert existing.saved is True


def test_a_first_time_push_with_no_known_etag_is_not_a_conflict():
    """No `remote_etag` at all (rule 3: back-compat) — the exact call shape
    `tests/test_caldav_writeback.py::test_push_update_overwrites_existing`
    already exercises, unmodified by this batch."""
    existing = FakeEvent()
    cal = FakeCalendar(REMOTE_URL, existing=existing)
    res = push_event([cal], CAL_ID, _ev(summary="Moved"), delete=False)
    assert res["ok"] is True and res.get("updated") is True
    assert existing.saved is True


def test_deleting_something_edited_elsewhere_is_also_a_conflict():
    existing = FakeEvent(etag='"server-etag-2"')
    cal = FakeCalendar(REMOTE_URL, existing=existing)
    local = _ev(remote_etag='"server-etag-1"')
    res = push_event([cal], CAL_ID, local, delete=True)
    assert res["ok"] is False and res["conflict"] is True
    assert existing.deleted is False


# ── 2. _persist_writeback_result marks the row, doesn't clear it ──────────

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(f"sqlite:///{_TMPDB.name}", connect_args={"check_same_thread": False},
                        poolclass=NullPool)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def _bind_session_local():
    """Re-bind on every test, not once at import time: `test_caldav_
    writeback_route.py` (ajeno) rebinds these same two module attributes at
    ITS OWN import time, and whichever module import runs last inside one
    pytest process wins when both are collected together — an existing
    hazard in this repo's own test pattern, not something introduced here.
    An autouse fixture makes this file's tests correct regardless of
    collection order, without touching that other file.
    `_persist_writeback_result` does `from core.database import ...
    SessionLocal` INSIDE the function body, so it re-resolves
    `core.database.SessionLocal` on every call — patching the module
    attribute itself, not a name some other module imported from it, is
    what actually takes effect here."""
    original_croutes, original_cdb = croutes.SessionLocal, cdb.SessionLocal
    croutes.SessionLocal = _TS
    cdb.SessionLocal = _TS
    try:
        yield
    finally:
        croutes.SessionLocal = original_croutes
        cdb.SessionLocal = original_cdb


def _seed_event(owner: str, uid: str, *, pending: str | None = "update") -> str:
    cid = "caldav-" + uuid.uuid4().hex[:10]
    db = _TS()
    try:
        db.add(CalendarCal(id=cid, owner=owner, name="C", source="caldav"))
        db.add(CalendarEvent(uid=uid, calendar_id=cid, summary="Old local draft",
                             dtstart=datetime(2026, 6, 10, 14, 0),
                             dtend=datetime(2026, 6, 10, 15, 0),
                             remote_etag='"server-etag-1"', caldav_sync_pending=pending))
        db.commit()
    finally:
        db.close()
    return cid


def _seed_event_calendar_id(uid: str) -> str:
    db = _TS()
    try:
        row = db.query(CalendarEvent).filter(CalendarEvent.uid == uid).first()
        return row.calendar_id
    finally:
        db.close()


def test_a_successful_push_still_clears_pending_as_before():
    owner = "conn04-owner-2"
    uid = "evt-" + uuid.uuid4().hex[:8]
    _seed_event(owner, uid)
    cal_id = _seed_event_calendar_id(uid)
    result = {"ok": True, "updated": True, "remote_href": "https://x/evt.ics",
             "remote_etag": '"server-etag-2"'}
    _persist_writeback_result(owner, cal_id, uid, result, delete=False)
    db = _TS()
    try:
        row = db.query(CalendarEvent).filter(CalendarEvent.uid == uid).first()
        assert row.caldav_sync_pending is None
        assert row.remote_etag == '"server-etag-2"'
    finally:
        db.close()


def test_conflict_row_after_persist_is_marked_and_untouched():
    owner = "conn04-owner-3"
    uid = "evt-" + uuid.uuid4().hex[:8]
    _seed_event(owner, uid)
    cal_id = _seed_event_calendar_id(uid)
    result = {"ok": False, "conflict": True, "remote_etag": '"server-etag-2"'}
    _persist_writeback_result(owner, cal_id, uid, result, delete=False)
    db = _TS()
    try:
        row = db.query(CalendarEvent).filter(CalendarEvent.uid == uid).first()
        assert row.caldav_sync_pending == "conflict"
        # Local draft's own etag is left alone — we still don't know which
        # side should win, so nothing here pretends the push landed.
        assert row.remote_etag == '"server-etag-1"'
        assert row.summary == "Old local draft"
    finally:
        db.close()


# ── 3. routes: list conflicts, and resolve one by keeping local ───────────

def _req(user="conn04-owner-4"):
    return SimpleNamespace(state=SimpleNamespace(current_user=user))


def _endpoint(method, suffix):
    router = croutes.setup_calendar_routes()
    for r in router.routes:
        if getattr(r, "path", "").endswith(suffix) and method in getattr(r, "methods", set()):
            return r.endpoint
    raise RuntimeError(f"{method} *{suffix} not found")


async def test_conflicts_route_lists_only_flagged_events():
    owner = "conn04-owner-5"
    uid_conflict = "evt-" + uuid.uuid4().hex[:8]
    uid_clean = "evt-" + uuid.uuid4().hex[:8]
    _seed_event(owner, uid_conflict, pending="conflict")
    _seed_event(owner, uid_clean, pending=None)

    list_conflicts = _endpoint("GET", "/conflicts")
    out = await list_conflicts(_req(owner))
    uids = [c["uid"] for c in out["conflicts"]]
    assert uid_conflict in uids
    assert uid_clean not in uids


async def test_keep_local_forces_the_push_by_dropping_the_known_etag(monkeypatch):
    owner = "conn04-owner-6"
    uid = "evt-" + uuid.uuid4().hex[:8]
    _seed_event(owner, uid, pending="conflict")

    seen = {}

    async def _fake_push_event_update(owner_, uid_):
        db = _TS()
        try:
            row = db.query(CalendarEvent).filter(CalendarEvent.uid == uid_).first()
            seen["remote_etag_when_called"] = row.remote_etag
            seen["pending_when_called"] = row.caldav_sync_pending
        finally:
            db.close()
        return {"ok": True}

    import src.caldav_sync as csync
    monkeypatch.setattr(csync, "push_event_update", _fake_push_event_update)

    keep_local = _endpoint("POST", "/conflicts/{uid}/keep-local")
    out = await keep_local(uid, _req(owner))
    assert out["ok"] is True
    # The stale etag was cleared BEFORE the forced push, so `push_event`
    # will not detect a conflict against its own now-stale comparison.
    assert seen["remote_etag_when_called"] is None
    assert seen["pending_when_called"] == "update"
