"""tests/test_calendar_external_ref.py — F4.1: calendar idempotency.

`POST /api/calendar/events` accepts an optional `external_ref` (<=200
chars). A second POST from the same owner with the same `external_ref`
returns the existing event (`created: false`, HTTP 200) instead of creating
a duplicate. `GET /api/calendar/events?external_ref=...` looks the event up
directly, independent of any start/end window.
"""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from tests.helpers.import_state import clear_fake_database_modules
from tests.helpers.sqlite_db import make_temp_sqlite

clear_fake_database_modules()

import core.database as cdb
from core.database import CalendarEvent

_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)


def _request(owner="tester"):
    return SimpleNamespace(state=SimpleNamespace(current_user=owner))


def _route_endpoint(router, path, method):
    full_path = f"/api/calendar{path}"
    for route in router.routes:
        if route.path == full_path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route not found: {method} {full_path}")


@pytest.fixture
def _calendar_routes(monkeypatch):
    monkeypatch.delitem(sys.modules, "routes.calendar_routes", raising=False)
    import routes.calendar_routes as calendar_routes
    monkeypatch.setattr(calendar_routes, "SessionLocal", _TS)
    return calendar_routes


def test_second_post_with_same_external_ref_returns_existing_event(_calendar_routes):
    calendar_routes = _calendar_routes
    router = calendar_routes.setup_calendar_routes()
    create_event = _route_endpoint(router, "/events", "POST")
    owner = "alice-" + str(id(router))
    payload = calendar_routes.EventCreate(
        summary="Interview: Backend Engineer",
        dtstart="2026-09-20T10:30:00+02:00",
        external_ref="jobhunter:job-1:msg-1",
    )

    first = asyncio.run(create_event(_request(owner), payload))
    second = asyncio.run(create_event(_request(owner), payload))

    assert first["created"] is True
    assert second["created"] is False
    assert second["uid"] == first["uid"]

    db = _TS()
    try:
        rows = db.query(CalendarEvent).filter(CalendarEvent.external_ref == "jobhunter:job-1:msg-1").all()
        assert len(rows) == 1
    finally:
        db.close()


def test_external_ref_is_scoped_per_owner(_calendar_routes):
    calendar_routes = _calendar_routes
    router = calendar_routes.setup_calendar_routes()
    create_event = _route_endpoint(router, "/events", "POST")

    payload = calendar_routes.EventCreate(
        summary="Interview: Backend Engineer",
        dtstart="2026-09-20T10:30:00+02:00",
        external_ref="jobhunter:job-shared:msg-shared",
    )
    alice = asyncio.run(create_event(_request("alice-scope"), payload))
    bob = asyncio.run(create_event(_request("bob-scope"), payload))

    assert alice["created"] is True
    assert bob["created"] is True
    assert alice["uid"] != bob["uid"]


def test_get_events_by_external_ref_does_not_require_a_date_window(_calendar_routes):
    calendar_routes = _calendar_routes
    router = calendar_routes.setup_calendar_routes()
    create_event = _route_endpoint(router, "/events", "POST")
    list_events = _route_endpoint(router, "/events", "GET")
    owner = "carol-" + str(id(router))

    created = asyncio.run(create_event(_request(owner), calendar_routes.EventCreate(
        summary="Interview: Data Engineer",
        dtstart="2026-09-21T09:00:00+02:00",
        external_ref="jobhunter:job-2:msg-2",
    )))

    out = asyncio.run(list_events(_request(owner), external_ref="jobhunter:job-2:msg-2"))

    assert len(out["events"]) == 1
    assert out["events"][0]["uid"] == created["uid"]
    assert out["events"][0]["external_ref"] == "jobhunter:job-2:msg-2"


def test_get_events_by_external_ref_is_owner_scoped(_calendar_routes):
    calendar_routes = _calendar_routes
    router = calendar_routes.setup_calendar_routes()
    create_event = _route_endpoint(router, "/events", "POST")
    list_events = _route_endpoint(router, "/events", "GET")

    asyncio.run(create_event(_request("dave-scope"), calendar_routes.EventCreate(
        summary="Interview",
        dtstart="2026-09-22T09:00:00+02:00",
        external_ref="jobhunter:job-3:msg-3",
    )))

    out = asyncio.run(list_events(_request("eve-scope"), external_ref="jobhunter:job-3:msg-3"))
    assert out["events"] == []


def test_missing_external_ref_still_requires_start_and_end(_calendar_routes):
    calendar_routes = _calendar_routes
    from fastapi import HTTPException
    router = calendar_routes.setup_calendar_routes()
    list_events = _route_endpoint(router, "/events", "GET")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(list_events(_request("frank-scope")))
    assert exc.value.status_code == 400


def test_external_ref_too_long_is_rejected_by_the_model(_calendar_routes):
    calendar_routes = _calendar_routes
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        calendar_routes.EventCreate(
            summary="x",
            dtstart="2026-09-20T10:30:00+02:00",
            external_ref="x" * 201,
        )
