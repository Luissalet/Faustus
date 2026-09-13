"""tests/test_email_list_filters.py — F4.2.

`GET /api/email/list` gains optional `since`/`until` (ISO 8601, explicit
timezone required) and `unread_only` filters. Confirms:
  * the parser requires an explicit timezone (never silently assumes one),
  * the window/unread filter keeps exactly the matching rows,
  * the real route (driven through the existing email fixture mechanism,
    `data/fixture_email_messages.json` — same one other email tests build
    on) applies the filters without ever marking anything read.
"""
import json

import pytest
from fastapi import HTTPException

import routes.email_routes as email_routes
from routes.email_routes import _apply_email_list_window, _parse_since_until


def _route_endpoint(router, path: str, method: str):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


# --------------------------------------------------------------------------
# _parse_since_until
# --------------------------------------------------------------------------

def test_parse_since_until_none_and_blank_pass_through():
    assert _parse_since_until(None, "since") is None
    assert _parse_since_until("  ", "since") is None


def test_parse_since_until_requires_explicit_timezone():
    with pytest.raises(HTTPException) as exc:
        _parse_since_until("2026-09-01T00:00:00", "since")
    assert exc.value.status_code == 400


def test_parse_since_until_rejects_unparsable_value():
    with pytest.raises(HTTPException) as exc:
        _parse_since_until("not-a-date", "until")
    assert exc.value.status_code == 400


def test_parse_since_until_accepts_z_and_numeric_offset():
    from datetime import timezone
    z = _parse_since_until("2026-09-01T10:00:00Z", "since")
    off = _parse_since_until("2026-09-01T12:00:00+02:00", "since")
    assert z.tzinfo == timezone.utc
    assert z == off  # same absolute instant


# --------------------------------------------------------------------------
# _apply_email_list_window
# --------------------------------------------------------------------------

def _email(uid, epoch, is_read=False):
    return {"uid": uid, "date_epoch": epoch, "is_read": is_read}


def test_apply_window_noop_without_any_filter():
    resp = {"emails": [_email("1", 100)], "total": 1}
    out = _apply_email_list_window(resp, None, None, False)
    assert out is resp


def test_apply_window_filters_by_since_and_until():
    resp = {
        "emails": [
            _email("early", 1_000),
            _email("in_range", 2_000),
            _email("late", 3_000),
        ],
        "total": 3,
    }
    since_dt = _parse_since_until("1970-01-01T00:25:00Z", "since")   # epoch 1500
    until_dt = _parse_since_until("1970-01-01T00:42:00Z", "until")   # epoch 2520
    out = _apply_email_list_window(resp, since_dt, until_dt, False)
    assert [e["uid"] for e in out["emails"]] == ["in_range"]
    assert out["total"] == 1


def test_apply_window_unread_only_drops_read_messages():
    resp = {
        "emails": [_email("read", 100, is_read=True), _email("unread", 100, is_read=False)],
        "total": 2,
    }
    out = _apply_email_list_window(resp, None, None, True)
    assert [e["uid"] for e in out["emails"]] == ["unread"]


def test_apply_window_never_mutates_the_source_dict():
    resp = {"emails": [_email("a", 100, is_read=True)], "total": 1}
    out = _apply_email_list_window(resp, None, None, True)
    assert out is not resp
    assert resp["emails"] == [_email("a", 100, is_read=True)]  # untouched


# --------------------------------------------------------------------------
# Full route, through the existing fixture-email mechanism
# --------------------------------------------------------------------------

@pytest.fixture
def _fixture_inbox(tmp_path, monkeypatch):
    """Two fixture messages a week apart, wired through the SAME
    `data/fixture_email_messages.json` + `_fixture_email_list` path other
    email tests already rely on (routes/email_routes.py) — not a new
    fixture mechanism."""
    monkeypatch.setattr(email_routes, "_start_poller", lambda: None)
    monkeypatch.setattr(email_routes, "DATA_DIR", str(tmp_path))
    (tmp_path / "fixture_email_messages.json").write_text(
        json.dumps({
            "messages": [
                {
                    "owner": "tester",
                    "from": "Old Sender <old@example.com>",
                    "subject": "Old message",
                    "body": "This one is outside the window.",
                    "date": "2026-08-01T09:00:00+02:00",
                },
                {
                    "owner": "tester",
                    "from": "New Sender <new@example.com>",
                    "subject": "New message",
                    "body": "This one is inside the window.",
                    "date": "2026-09-05T09:00:00+02:00",
                },
            ]
        }),
        encoding="utf-8",
    )
    router = email_routes.setup_email_routes()
    return _route_endpoint(router, "/api/email/list", "GET")


@pytest.mark.asyncio
async def test_list_route_applies_since_until_window(_fixture_inbox):
    list_emails = _fixture_inbox
    result = await list_emails(
        folder="INBOX", limit=50, offset=0, filter="all", from_addr=None,
        account_id=None, has_attachments=0, cached_only=0, cache_bust=None,
        since="2026-09-01T00:00:00Z", until="2026-09-30T00:00:00Z",
        unread_only=False, owner="tester",
    )
    subjects = [e["subject"] for e in result["emails"]]
    assert subjects == ["New message"]
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_list_route_without_filters_returns_both_and_marks_nothing_read(_fixture_inbox):
    list_emails = _fixture_inbox
    result = await list_emails(
        folder="INBOX", limit=50, offset=0, filter="all", from_addr=None,
        account_id=None, has_attachments=0, cached_only=0, cache_bust=None,
        since=None, until=None, unread_only=False, owner="tester",
    )
    assert result["total"] == 2
    assert all(e["is_read"] is False for e in result["emails"])


@pytest.mark.asyncio
async def test_list_route_rejects_naive_since(_fixture_inbox):
    list_emails = _fixture_inbox
    with pytest.raises(HTTPException) as exc:
        await list_emails(
            folder="INBOX", limit=50, offset=0, filter="all", from_addr=None,
            account_id=None, has_attachments=0, cached_only=0, cache_bust=None,
            since="2026-09-01T00:00:00", until=None, unread_only=False, owner="tester",
        )
    assert exc.value.status_code == 400
