"""Google Calendar OAuth2 + Calendar API v3 sync — Lote G1.

Covers:
  - `src/google_calendar_accounts.py` — encrypted-at-rest account storage,
    token refresh (fresh/expired/invalid_grant), client_configured().
  - `routes/calendar_routes.py`'s new `/oauth/google/*` and `/config/google*`
    routes (G1.2) — pulled directly out of the router and called, same
    pattern `tests/test_caldav_writeback_route.py` and `test_email_oauth.py`
    already use (no TestClient/ASGI boot; a real Google is never contacted —
    outbound HTTP is mocked or run against `httpx.MockTransport`).
  - `src/google_calendar_sync.py` — pull (calendars + timed/all-day/
    recurring/cancelled/exceptional-instance events, incremental syncToken,
    410 full-resync), push (create/update/conflict/delete), token-refresh
    retry on 401, `push_pending`.
  - `POST /api/calendar/sync` aggregating both providers (G1.4).
  - The existing CalDAV suite staying green is asserted separately by the
    orchestrator (`grep -ln caldav tests/*.py`); this file only adds
    coverage, it does not touch CalDAV code paths.

No live network: OAuth token/userinfo calls are mocked via
`mock.patch("httpx.post"/"httpx.get")` (mirrors `tests/test_email_oauth.py`);
Calendar API v3 pull/push calls run through a monkeypatched
`google_calendar_sync._CLIENT_FACTORY` returning
`httpx.Client(transport=httpx.MockTransport(handler))`.
"""

import os
import tempfile
import time
import unittest.mock as mock
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import routes.calendar_routes as croutes
import src.google_calendar_accounts as gaccounts
import src.google_calendar_sync as gsync
from core.database import CalendarCal, CalendarEvent
from src.secret_storage import decrypt as _dec, encrypt as _enc


# ── DB isolation ─────────────────────────────────────────────────────────

@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    engine = create_engine(
        f"sqlite:///{tmp.name}", connect_args={"check_same_thread": False}, poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setattr(croutes, "SessionLocal", factory)
    yield factory
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


# ── prefs isolation (google_calendar_accounts storage) ─────────────────────

@pytest.fixture
def prefs(monkeypatch):
    import routes.prefs_routes as prefs_routes

    store: dict = {}

    def _load(owner=None):
        return dict(store.get(owner or "", {}))

    def _save(owner, data):
        store[owner or ""] = dict(data)

    monkeypatch.setattr(prefs_routes, "_load_for_user", _load)
    monkeypatch.setattr(prefs_routes, "_save_for_user", _save)
    return store


@pytest.fixture(autouse=True)
def google_client_env(monkeypatch):
    """A configured OAuth client by default; individual tests override."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.delenv("GOOGLE_CALENDAR_OAUTH_REDIRECT_URI", raising=False)


def _make_account(prefs, owner="tester", account_id="acct-g1", email="luis@gmail.com",
                   access_token="ya29.live", refresh_token="1//refresh",
                   expiry=None, status="ok", selected_calendars=None, sync_tokens=None):
    acc = {
        "id": account_id,
        "label": f"Google · {email}",
        "email": email,
        "access_token": _enc(access_token) if access_token else "",
        "refresh_token": _enc(refresh_token) if refresh_token else "",
        "token_expiry": str(int(time.time()) + 3600) if expiry is None else str(expiry),
        "status": status,
        "sync_tokens": sync_tokens or {},
        "selected_calendars": selected_calendars,
        "created_at": time.time(),
        "last_sync_at": None,
    }
    gaccounts.upsert_account(owner, acc)
    return acc


def _use_mock_transport(monkeypatch, handler):
    def factory():
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=15)

    monkeypatch.setattr(gsync, "_CLIENT_FACTORY", factory)


# ── OAuth routes ─────────────────────────────────────────────────────────

class _FakeRequest:
    def __init__(self, scheme="http", host="localhost:7000", user="tester"):
        self.headers = {"host": host}
        self.url = SimpleNamespace(scheme=scheme)
        self.state = SimpleNamespace(current_user=user)


def _route(method, path_suffix):
    router = croutes.setup_calendar_routes()
    for r in router.routes:
        if getattr(r, "path", "").endswith(path_suffix) and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(f"{method} *{path_suffix} not found")


def _location(resp):
    return resp.headers["location"]


async def test_authorize_without_client_configured_returns_400(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    authorize = _route("GET", "/oauth/google/authorize")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await authorize(request=_FakeRequest(), account_id="")
    assert exc.value.status_code == 400
    # G3.1: the message points at the in-app setup wizard now, not a raw
    # env var name — see src/google_oauth_client.py::NOT_CONFIGURED_MESSAGE.
    assert "Settings" in exc.value.detail
    assert "Integrations" in exc.value.detail


async def test_authorize_builds_url_with_scopes_and_valid_state():
    import urllib.parse
    from routes.email_helpers import verify_oauth_state

    authorize = _route("GET", "/oauth/google/authorize")
    resp = await authorize(request=_FakeRequest(host="odysseus.example.ts.net:7443"), account_id="")
    loc = resp.headers["location"]
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    assert query["scope"] == ["https://www.googleapis.com/auth/calendar openid email"]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["redirect_uri"] == ["http://odysseus.example.ts.net:7443/api/calendar/oauth/google/callback"]
    payload = verify_oauth_state(query["state"][0])
    assert payload is not None
    assert payload["a"] == "calendar"
    assert payload["o"] == "tester"


async def test_callback_missing_code_redirects_generic_error():
    callback = _route("GET", "/oauth/google/callback")
    resp = await callback(request=_FakeRequest(), code="", state="", error="")
    assert "calendar_oauth_error=missing_code" in _location(resp)


async def test_callback_invalid_state_redirects_generic_error():
    callback = _route("GET", "/oauth/google/callback")
    resp = await callback(request=_FakeRequest(), code="4/code", state="not-a-valid-state", error="")
    loc = _location(resp)
    assert "calendar_oauth_error=invalid_state" in loc
    assert "4/code" not in loc


async def test_callback_provider_error_redirects_generic_error():
    callback = _route("GET", "/oauth/google/callback")
    resp = await callback(request=_FakeRequest(), code="", state="", error="access_denied")
    loc = _location(resp)
    assert "calendar_oauth_error=google_error" in loc
    assert "access_denied" not in loc


async def test_callback_redirects_to_studio_integrations_route():
    """The redirect target is the Studio's REAL Integrations route
    (`/settings?s=integrations` — studio/src/screens/Settings.tsx's
    SECTIONS/`s` query param), not the legacy `/?section=integrations`
    routes/email_routes.py's pre-Studio callback still uses."""
    from routes.email_helpers import make_oauth_state

    callback = _route("GET", "/oauth/google/callback")
    token_resp = mock.MagicMock()
    token_resp.raise_for_status = mock.MagicMock()
    token_resp.json.return_value = {"access_token": "ya29.t", "refresh_token": "1//r", "expires_in": 3600}
    userinfo_resp = mock.MagicMock()
    userinfo_resp.is_success = True
    userinfo_resp.json.return_value = {"email": "luis@gmail.com"}

    state = make_oauth_state("calendar", "tester")
    with mock.patch("httpx.post", return_value=token_resp), \
         mock.patch("httpx.get", return_value=userinfo_resp):
        resp = await callback(request=_FakeRequest(), code="4/code", state=state, error="")
    loc = _location(resp)
    assert loc.startswith("/settings?s=integrations"), loc
    assert "calendar_oauth=ok" in loc


async def test_callback_saves_account_encrypted_and_never_returns_plaintext(prefs):
    from routes.email_helpers import make_oauth_state

    callback = _route("GET", "/oauth/google/callback")
    raw_access, raw_refresh = "ya29.super_secret", "1//super_secret_refresh"
    token_resp = mock.MagicMock()
    token_resp.raise_for_status = mock.MagicMock()
    token_resp.json.return_value = {"access_token": raw_access, "refresh_token": raw_refresh, "expires_in": 3600}
    userinfo_resp = mock.MagicMock()
    userinfo_resp.is_success = True
    userinfo_resp.json.return_value = {"email": "luis@gmail.com"}

    state = make_oauth_state("calendar", "tester")
    with mock.patch("httpx.post", return_value=token_resp), \
         mock.patch("httpx.get", return_value=userinfo_resp):
        resp = await callback(request=_FakeRequest(), code="4/code", state=state, error="")
    assert "calendar_oauth=ok" in _location(resp)

    # Never in the raw prefs store either.
    blob = str(prefs)
    assert raw_access not in blob
    assert raw_refresh not in blob

    accounts_raw = gaccounts.list_accounts("tester", public=False)
    assert len(accounts_raw) == 1
    assert _dec(accounts_raw[0]["access_token"]) == raw_access
    assert _dec(accounts_raw[0]["refresh_token"]) == raw_refresh

    get_config = _route("GET", "/config/google")
    result = await get_config(request=_FakeRequest())
    assert result["configured"] is True
    assert len(result["accounts"]) == 1
    acct = result["accounts"][0]
    assert acct["email"] == "luis@gmail.com"
    assert "access_token" not in acct
    assert "refresh_token" not in acct
    import json as _json
    assert raw_access not in _json.dumps(result)
    assert raw_refresh not in _json.dumps(result)


async def test_delete_google_account_route(prefs):
    _make_account(prefs)
    delete_ep = _route("DELETE", "/config/google/{account_id}")
    with mock.patch("httpx.post", return_value=mock.MagicMock(status_code=200)):
        result = await delete_ep(account_id="acct-g1", request=_FakeRequest())
    assert result == {"ok": True}
    assert gaccounts.get_account("tester", "acct-g1") is None


# ── google_calendar_accounts: token refresh ─────────────────────────────

def test_access_token_for_uses_cached_token_when_fresh(prefs):
    _make_account(prefs, expiry=int(time.time()) + 3600)
    with mock.patch("httpx.post") as post:
        token = gaccounts.access_token_for("tester", "acct-g1")
    assert token == "ya29.live"
    post.assert_not_called()


def test_access_token_for_refreshes_when_expired(prefs):
    _make_account(prefs, expiry=int(time.time()) - 10)
    resp = mock.MagicMock(status_code=200)
    resp.json.return_value = {"access_token": "ya29.new", "expires_in": 3600}
    with mock.patch("httpx.post", return_value=resp) as post:
        token = gaccounts.access_token_for("tester", "acct-g1")
    assert token == "ya29.new"
    post.assert_called_once()
    stored = gaccounts.get_account("tester", "acct-g1", public=False)
    assert _dec(stored["access_token"]) == "ya29.new"
    assert stored["status"] == "ok"


def test_access_token_for_invalid_grant_marks_needs_reauth_no_token_in_error(prefs, caplog):
    _make_account(prefs, expiry=int(time.time()) - 10)
    resp = mock.MagicMock(status_code=400)
    resp.json.return_value = {"error": "invalid_grant"}
    with mock.patch("httpx.post", return_value=resp):
        token = gaccounts.access_token_for("tester", "acct-g1")
    assert token is None
    stored = gaccounts.get_account("tester", "acct-g1", public=False)
    assert stored["status"] == "needs_reauth"
    assert "ya29.live" not in caplog.text
    assert "refresh" not in caplog.text.lower() or "1//refresh" not in caplog.text


def test_client_configured_false_without_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    assert gaccounts.client_configured() is False


# ── pull ─────────────────────────────────────────────────────────────────

def _events_list_response(items, next_page_token=None, next_sync_token=None):
    body = {"items": items}
    if next_page_token:
        body["nextPageToken"] = next_page_token
    if next_sync_token:
        body["nextSyncToken"] = next_sync_token
    return httpx.Response(200, json=body)


async def test_pull_creates_calendar_and_maps_event_shapes(db, prefs, monkeypatch):
    _make_account(prefs)
    items = [
        {
            "id": "evt-timed", "etag": '"e1"', "status": "confirmed",
            "summary": "Standup", "description": "daily", "location": "Zoom",
            "start": {"dateTime": "2026-06-10T09:00:00-04:00"},
            "end": {"dateTime": "2026-06-10T09:30:00-04:00"},
        },
        {
            "id": "evt-allday", "etag": '"e2"', "status": "confirmed",
            "summary": "Conference",
            "start": {"date": "2026-06-11"}, "end": {"date": "2026-06-12"},
        },
        {
            "id": "evt-series", "etag": '"e3"', "status": "confirmed",
            "summary": "Weekly sync",
            "start": {"dateTime": "2026-06-08T10:00:00-04:00"},
            "end": {"dateTime": "2026-06-08T10:30:00-04:00"},
            "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=5"],
        },
    ]

    def handler(request):
        if request.url.path.endswith("/users/me/calendarList"):
            return httpx.Response(200, json={"items": [
                {"id": "primary", "summary": "Primary", "primary": True},
            ]})
        if request.url.path.endswith("/events") and request.method == "GET":
            return _events_list_response(items, next_sync_token="synctok-1")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    _use_mock_transport(monkeypatch, handler)

    with mock.patch("httpx.post"):  # no token refresh should be attempted
        result = await gsync.pull("tester")

    assert result["errors"] == []
    assert result["calendars"] == 1
    assert result["events"] == 3

    session = db()
    try:
        cal = session.query(CalendarCal).filter(CalendarCal.owner == "tester").first()
        assert cal is not None
        assert cal.source == "google"
        assert cal.account_id == "acct-g1"

        timed = session.query(CalendarEvent).filter(CalendarEvent.uid == "google:acct-g1:evt-timed").first()
        assert timed is not None
        assert timed.all_day is False
        assert timed.is_utc is True
        assert timed.dtstart.hour == 13  # 09:00-04:00 -> 13:00 UTC
        assert timed.remote_href == "evt-timed"
        assert timed.remote_etag == "e1"

        allday = session.query(CalendarEvent).filter(CalendarEvent.uid == "google:acct-g1:evt-allday").first()
        assert allday is not None
        assert allday.all_day is True

        series = session.query(CalendarEvent).filter(CalendarEvent.uid == "google:acct-g1:evt-series").first()
        assert series is not None
        assert series.rrule == "FREQ=WEEKLY;COUNT=5"
    finally:
        session.close()

    stored = gaccounts.get_account("tester", "acct-g1", public=False)
    assert stored["sync_tokens"]["primary"] == "synctok-1"
    assert stored["last_sync_at"] is not None


async def test_pull_deletes_cancelled_event(db, prefs, monkeypatch):
    _make_account(prefs)

    def handler(request):
        if request.url.path.endswith("/users/me/calendarList"):
            return httpx.Response(200, json={"items": [{"id": "primary", "primary": True}]})
        if request.url.path.endswith("/events"):
            return _events_list_response([{
                "id": "evt-gone", "status": "cancelled",
            }], next_sync_token="tok")
        raise AssertionError("unexpected request")

    session = db()
    try:
        session.add(CalendarCal(
            id=gsync._stable_cal_id("tester", "acct-g1", "primary"), owner="tester",
            name="Primary", source="google", account_id="acct-g1", caldav_base_url="primary",
        ))
        session.add(CalendarEvent(
            uid="google:acct-g1:evt-gone",
            calendar_id=gsync._stable_cal_id("tester", "acct-g1", "primary"),
            summary="Old", dtstart=__import__("datetime").datetime(2026, 6, 1, 9),
            dtend=__import__("datetime").datetime(2026, 6, 1, 10),
        ))
        session.commit()
    finally:
        session.close()

    _use_mock_transport(monkeypatch, handler)
    result = await gsync.pull("tester")
    assert result["deleted"] == 1

    session = db()
    try:
        assert session.query(CalendarEvent).filter(CalendarEvent.uid == "google:acct-g1:evt-gone").first() is None
    finally:
        session.close()


async def test_pull_cancelled_instance_becomes_master_exdate(db, prefs, monkeypatch):
    _make_account(prefs)
    cal_id = gsync._stable_cal_id("tester", "acct-g1", "primary")

    session = db()
    try:
        session.add(CalendarCal(id=cal_id, owner="tester", name="Primary", source="google",
                                 account_id="acct-g1", caldav_base_url="primary"))
        import datetime as _dt
        session.add(CalendarEvent(
            uid="google:acct-g1:evt-master", calendar_id=cal_id, summary="Weekly",
            dtstart=_dt.datetime(2026, 6, 1, 9), dtend=_dt.datetime(2026, 6, 1, 10),
            rrule="FREQ=WEEKLY;COUNT=5",
        ))
        session.commit()
    finally:
        session.close()

    def handler(request):
        if request.url.path.endswith("/users/me/calendarList"):
            return httpx.Response(200, json={"items": [{"id": "primary", "primary": True}]})
        if request.url.path.endswith("/events"):
            return _events_list_response([{
                "id": "evt-instance", "status": "cancelled",
                "recurringEventId": "evt-master",
                "originalStartTime": {"dateTime": "2026-06-08T09:00:00-04:00"},
            }], next_sync_token="tok")
        raise AssertionError("unexpected request")

    _use_mock_transport(monkeypatch, handler)
    await gsync.pull("tester")

    session = db()
    try:
        master = session.query(CalendarEvent).filter(CalendarEvent.uid == "google:acct-g1:evt-master").first()
        assert master is not None
        assert "2026-06-08T13:00" in (master.recurrence_exdates or "")
    finally:
        session.close()


async def test_pull_second_pass_with_sync_token_uses_incremental_params(db, prefs, monkeypatch):
    _make_account(prefs, sync_tokens={"primary": "old-token"})
    seen_params = []

    def handler(request):
        if request.url.path.endswith("/users/me/calendarList"):
            return httpx.Response(200, json={"items": [{"id": "primary", "primary": True}]})
        if request.url.path.endswith("/events"):
            seen_params.append(dict(request.url.params))
            return _events_list_response([], next_sync_token="new-token")
        raise AssertionError("unexpected request")

    _use_mock_transport(monkeypatch, handler)
    await gsync.pull("tester")

    assert seen_params[0].get("syncToken") == "old-token"
    assert "timeMin" not in seen_params[0]
    stored = gaccounts.get_account("tester", "acct-g1", public=False)
    assert stored["sync_tokens"]["primary"] == "new-token"


async def test_pull_410_drops_token_and_does_full_resync(db, prefs, monkeypatch):
    _make_account(prefs, sync_tokens={"primary": "stale-token"})
    calls = []

    def handler(request):
        if request.url.path.endswith("/users/me/calendarList"):
            return httpx.Response(200, json={"items": [{"id": "primary", "primary": True}]})
        if request.url.path.endswith("/events"):
            params = dict(request.url.params)
            calls.append(params)
            if params.get("syncToken") == "stale-token":
                return httpx.Response(410, json={"error": {"message": "Gone"}})
            assert "timeMin" in params  # the full-resync retry
            return _events_list_response([], next_sync_token="fresh-token")
        raise AssertionError("unexpected request")

    _use_mock_transport(monkeypatch, handler)
    result = await gsync.pull("tester")

    assert result["errors"] == []
    assert len(calls) == 2
    stored = gaccounts.get_account("tester", "acct-g1", public=False)
    assert stored["sync_tokens"]["primary"] == "fresh-token"


async def test_pull_needs_reauth_account_is_skipped_with_error(db, prefs):
    _make_account(prefs, status="needs_reauth")
    result = await gsync.pull("tester")
    assert result["calendars"] == 0
    assert any("needs reauth" in e for e in result["errors"])


# ── push ─────────────────────────────────────────────────────────────────

def _seed_google_event(db, *, uid="google:acct-g1:evt-1", remote_href=None, remote_etag=None,
                        pending="create", account_id="acct-g1", gcal_id="primary"):
    import datetime as _dt

    cal_id = gsync._stable_cal_id("tester", account_id, gcal_id)
    session = db()
    try:
        if not session.query(CalendarCal).filter(CalendarCal.id == cal_id).first():
            session.add(CalendarCal(id=cal_id, owner="tester", name="Primary", source="google",
                                     account_id=account_id, caldav_base_url=gcal_id))
        session.add(CalendarEvent(
            uid=uid, calendar_id=cal_id, summary="Push me",
            dtstart=_dt.datetime(2026, 7, 1, 9), dtend=_dt.datetime(2026, 7, 1, 10),
            is_utc=True, remote_href=remote_href, remote_etag=remote_etag,
            caldav_sync_pending=pending,
        ))
        session.commit()
    finally:
        session.close()
    return cal_id


async def test_push_create_saves_remote_id_and_etag(db, prefs, monkeypatch):
    _make_account(prefs)
    _seed_google_event(db, remote_href=None, remote_etag=None)

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/events"):
            return httpx.Response(200, json={"id": "new-gcal-id", "etag": '"etag-1"'})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    _use_mock_transport(monkeypatch, handler)
    result = await gsync.push_event_create("tester", "google:acct-g1:evt-1")
    assert result["ok"] is True
    assert result["remote_href"] == "new-gcal-id"

    session = db()
    try:
        ev = session.query(CalendarEvent).filter(CalendarEvent.uid == "google:acct-g1:evt-1").first()
        assert ev.remote_href == "new-gcal-id"
        assert ev.remote_etag == "etag-1"
        assert ev.caldav_sync_pending is None
    finally:
        session.close()


async def test_push_update_stale_etag_returns_conflict(db, prefs, monkeypatch):
    _make_account(prefs)
    _seed_google_event(db, remote_href="evt-1", remote_etag="old-etag", pending="update")

    def handler(request):
        if request.method == "PATCH":
            assert request.headers.get("If-Match") == '"old-etag"'
            return httpx.Response(412, json={"error": "etag mismatch"})
        raise AssertionError("unexpected request")

    _use_mock_transport(monkeypatch, handler)
    result = await gsync.push_event_update("tester", "google:acct-g1:evt-1")
    assert result.get("conflict") is True

    session = db()
    try:
        ev = session.query(CalendarEvent).filter(CalendarEvent.uid == "google:acct-g1:evt-1").first()
        assert ev.caldav_sync_pending == "conflict"
    finally:
        session.close()


async def test_push_delete_already_gone_is_ok(db, prefs, monkeypatch):
    _make_account(prefs)
    _seed_google_event(db, remote_href="evt-1", remote_etag="e1", pending=None)

    def handler(request):
        if request.method == "DELETE":
            return httpx.Response(404)
        raise AssertionError("unexpected request")

    _use_mock_transport(monkeypatch, handler)
    result = await gsync.push_event_delete("tester", "google:acct-g1:evt-1")
    assert result["ok"] is True


async def test_push_401_refreshes_token_once_and_retries(db, prefs, monkeypatch):
    _make_account(prefs, access_token="ya29.stale", expiry=int(time.time()) + 3600)
    _seed_google_event(db, remote_href=None, remote_etag=None)

    calls = {"n": 0}

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/events"):
            calls["n"] += 1
            if request.headers.get("Authorization") == "Bearer ya29.stale":
                return httpx.Response(401, json={"error": "unauthorized"})
            assert request.headers.get("Authorization") == "Bearer ya29.refreshed"
            return httpx.Response(200, json={"id": "gcal-id", "etag": '"e1"'})
        raise AssertionError("unexpected request")

    _use_mock_transport(monkeypatch, handler)

    refresh_resp = mock.MagicMock(status_code=200)
    refresh_resp.json.return_value = {"access_token": "ya29.refreshed", "expires_in": 3600}
    with mock.patch("httpx.post", return_value=refresh_resp):
        result = await gsync.push_event_create("tester", "google:acct-g1:evt-1")

    assert result["ok"] is True
    assert calls["n"] == 2


async def test_push_needs_reauth_when_no_token(db, prefs):
    _make_account(prefs, status="needs_reauth")
    _seed_google_event(db)
    result = await gsync.push_event_create("tester", "google:acct-g1:evt-1")
    assert result["ok"] is False
    assert result["error"] == "needs_reauth"


# ── /api/calendar/sync aggregation ──────────────────────────────────────

async def test_sync_endpoint_aggregates_caldav_and_google(db, prefs, monkeypatch):
    async def fake_caldav(owner, direction="pull"):
        return {"calendars": 1, "events": 2, "deleted": 0, "errors": ["caldav oops"]}

    async def fake_google(owner, direction="pull"):
        return {"calendars": 1, "events": 3, "deleted": 1, "errors": []}

    import src.caldav_sync as csync
    monkeypatch.setattr(csync, "sync_caldav_direction", fake_caldav)
    monkeypatch.setattr(gsync, "sync_google_direction", fake_google)

    sync_ep = _route("POST", "/sync")
    result = await sync_ep(request=_FakeRequest(), direction="pull")

    assert result["calendars"] == 2
    assert result["events"] == 5
    assert result["deleted"] == 1
    assert result["errors"] == ["caldav oops"]
    assert result["caldav"]["events"] == 2
    assert result["google"]["events"] == 3
