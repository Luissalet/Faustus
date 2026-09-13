"""Google Calendar API v3 <-> local SQLite sync.

Mirrors ``src/caldav_sync.py`` / ``src/caldav_writeback.py``'s shape (pull,
push_event_create/update/delete, push_pending, sync_*_direction) so
``routes/calendar_routes.py`` can dispatch to whichever module a calendar's
``source`` names without special-casing either provider.

Google rejects Basic Auth for Calendar — this talks to the REST v3 HTTP API
directly with a Bearer token (``src.google_calendar_accounts.access_token_for``)
over ``httpx``, no new dependency (``google-api-python-client`` is
deliberately NOT used, per the contract).

Design notes, mirroring caldav_sync's:
- Each Google account can have several Google calendars; each maps to one
  local ``CalendarCal`` row (``source="google"``, ``account_id`` = the Google
  account's id, ``id`` = a stable hash of (owner, account_id, google_calendar_id)
  so re-syncs idempotently target the same row — see ``_stable_cal_id``).
  The Google calendar id itself is stored in ``caldav_base_url`` (a generic
  "remote calendar identifier" string column caldav_sync already uses the
  same way for its remote URL — reused here rather than adding a column).
- Events upsert by a uid of the form ``google:{account_id}:{event_id}``.
  ``remote_href`` holds the Google event id, ``remote_etag`` the Google
  ``etag`` (used for optimistic-concurrency ``If-Match`` on push, exactly
  like caldav_writeback's CONN-04 conflict handling).
- An incremental sync uses Google's ``syncToken`` (stored per-calendar on the
  account, ``sync_tokens``); a 410 (token expired server-side) drops it and
  redoes that one calendar as a full window sync.
- A cancelled *exceptional instance* of a recurring event (``recurringEventId``
  set, ``status="cancelled"``) becomes an EXDATE on the local master series,
  matching how CalDAV EXDATEs are represented locally. A cancelled *master*
  event is deleted locally. A modified (non-cancelled) exceptional instance
  is materialized as its own one-off local event (``origin="google-instance"``)
  — Faustus's local model has no first-class "modified occurrence of a
  series" concept beyond RRULE+EXDATE, so this is a deliberate, documented
  approximation: it shows up and can be edited/deleted like any other event,
  but is not visually linked back to its parent series.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 90
_LOOKAHEAD_DAYS = 365
_API_BASE = "https://www.googleapis.com/calendar/v3"

# Monkeypatchable client factory — tests substitute
# ``httpx.Client(transport=httpx.MockTransport(handler))`` so pull/push run
# against a fake Google with no real network access.
_CLIENT_FACTORY = None


def _client():
    import httpx

    if _CLIENT_FACTORY is not None:
        return _CLIENT_FACTORY()
    return httpx.Client(timeout=15)


class _GoogleGoneError(Exception):
    """Raised internally when Google answers 410 to a syncToken request."""


def _stable_cal_id(owner: str, account_id: str, gcal_id: str) -> str:
    """Deterministic local id for a remote Google calendar, scoped to owner
    and account (mirrors ``caldav_sync._stable_cal_id``)."""
    import hashlib

    key = f"{owner}\n{account_id}\n{gcal_id}"
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"google-{h}"


# ── datetime / recurrence mapping ──────────────────────────────────────────

def _parse_gtime(node: dict) -> tuple:
    """Return (naive datetime, all_day, is_utc) from a Google start/end node.

    ``{"date": "YYYY-MM-DD"}`` is all-day. ``{"dateTime": "...", "timeZone": ...}``
    always carries an explicit UTC offset from Google, so timed events are
    always converted to naive-UTC with ``is_utc=True`` (no "floating" case).
    """
    if "date" in node:
        y, m, d = (int(x) for x in str(node["date"]).split("-"))
        return datetime(y, m, d), True, False
    raw = str(node.get("dateTime") or "")
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None), False, True
    return dt, False, False


def _rrule_and_exdates(recurrence: list) -> tuple:
    rrule = ""
    exdates: list = []
    for line in recurrence or []:
        upper = str(line).upper()
        if upper.startswith("RRULE:"):
            rrule = str(line)[len("RRULE:"):]
        elif upper.startswith("EXDATE"):
            exdates.extend(_parse_exdate_line(str(line)))
    return rrule, exdates


def _parse_exdate_line(line: str) -> list:
    """``EXDATE[;TZID=...][;VALUE=DATE]:val1,val2`` -> occurrence keys in the
    same shape ``_occurrence_exdate_key`` (routes/calendar_routes.py) stores:
    ``YYYY-MM-DD`` for a date-only value, ``YYYY-MM-DDTHH:MM`` for a
    date-time one."""
    try:
        _, _, values = line.partition(":")
        out = []
        for raw in values.split(","):
            raw = raw.strip()
            if not raw:
                continue
            if len(raw) == 8 and raw.isdigit():
                out.append(f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}")
                continue
            raw = raw.rstrip("Zz")
            date_part, _, time_part = raw.partition("T")
            if len(date_part) != 8 or len(time_part) < 4:
                continue
            out.append(f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]}T{time_part[0:2]}:{time_part[2:4]}")
        return out
    except Exception:
        return []


def _exdate_line(key: str, all_day: bool) -> str:
    if all_day or len(key) == 10:
        return f"EXDATE;VALUE=DATE:{key.replace('-', '')}"
    date_part, _, time_part = key.partition("T")
    return f"EXDATE:{date_part.replace('-', '')}T{time_part.replace(':', '')}00Z"


# ── pull (remote -> local) ─────────────────────────────────────────────────

async def pull(owner: str) -> dict:
    import asyncio

    return await asyncio.to_thread(_pull_blocking, owner)


def _pull_blocking(owner: str) -> dict:
    from src.google_calendar_accounts import list_accounts, access_token_for

    result = {"calendars": 0, "events": 0, "deleted": 0, "errors": []}
    accounts = list_accounts(owner, public=False)
    if not accounts:
        result["errors"].append("Google Calendar is not configured")
        return result

    client = _client()
    try:
        for acc in accounts:
            account_id = acc.get("id") or ""
            label = acc.get("label") or acc.get("email") or account_id
            if acc.get("status") == "needs_reauth":
                result["errors"].append(f"{label}: needs reauth — reconnect this account")
                continue
            token = access_token_for(owner, account_id)
            if not token:
                result["errors"].append(f"{label}: no valid access token (needs reauth or not configured)")
                continue
            try:
                _pull_account(owner, acc, token, client, result)
            except Exception as e:
                logger.exception("Google Calendar pull failed for account %s", label)
                result["errors"].append(f"{label}: {str(e)[:200]}")
    finally:
        client.close()
    return result


def _list_calendars(client, token: str) -> list:
    items = []
    page_token = None
    while True:
        params = {"maxResults": 250}
        if page_token:
            params["pageToken"] = page_token
        resp = client.get(
            f"{_API_BASE}/users/me/calendarList",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return items


def _pull_account(owner: str, acc: dict, token: str, client, result: dict) -> None:
    from core.database import CalendarCal, SessionLocal
    from src.google_calendar_accounts import upsert_account

    account_id = acc.get("id") or ""
    selected = acc.get("selected_calendars")
    sync_tokens = dict(acc.get("sync_tokens") or {})

    cal_items = _list_calendars(client, token)
    db = SessionLocal()
    try:
        for gcal in cal_items:
            gcal_id = gcal.get("id") or ""
            if not gcal_id:
                continue
            is_primary = bool(gcal.get("primary"))
            if selected is not None and gcal_id not in selected and not is_primary:
                continue

            local_id = _stable_cal_id(owner, account_id, gcal_id)
            local_cal = db.query(CalendarCal).filter(
                CalendarCal.id == local_id, CalendarCal.owner == owner,
            ).first()
            display_name = gcal.get("summary") or gcal_id
            color = gcal.get("backgroundColor") or "#4285f4"
            if not local_cal:
                local_cal = CalendarCal(
                    id=local_id, owner=owner, name=display_name, color=color,
                    source="google", account_id=account_id, caldav_base_url=gcal_id,
                )
                db.add(local_cal)
                db.commit()
            else:
                changed = False
                if local_cal.name != display_name:
                    local_cal.name = display_name
                    changed = True
                if local_cal.caldav_base_url != gcal_id:
                    local_cal.caldav_base_url = gcal_id
                    changed = True
                if local_cal.account_id != account_id:
                    local_cal.account_id = account_id
                    changed = True
                if changed:
                    db.commit()
            result["calendars"] += 1

            token_used = sync_tokens.get(gcal_id)
            try:
                new_token, ev_count, del_count = _sync_calendar_events(
                    db, owner, local_cal.id, client, token, account_id, gcal_id, token_used,
                )
                sync_tokens[gcal_id] = new_token
                result["events"] += ev_count
                result["deleted"] += del_count
            except _GoogleGoneError:
                sync_tokens.pop(gcal_id, None)
                try:
                    new_token, ev_count, del_count = _sync_calendar_events(
                        db, owner, local_cal.id, client, token, account_id, gcal_id, None,
                    )
                    sync_tokens[gcal_id] = new_token
                    result["events"] += ev_count
                    result["deleted"] += del_count
                except Exception as e2:
                    result["errors"].append(f"{display_name}: resync after 410 failed ({str(e2)[:160]})")
            except Exception as e:
                result["errors"].append(f"{display_name}: {str(e)[:200]}")
    finally:
        db.close()

    acc = dict(acc)
    acc["sync_tokens"] = sync_tokens
    acc["last_sync_at"] = time.time()
    upsert_account(owner, acc)


def _sync_calendar_events(db, owner, local_cal_id, client, token, account_id, gcal_id, sync_token):
    events_seen = 0
    deleted = 0
    page_token = None
    next_sync_token = sync_token
    pending: dict = {}
    while True:
        params = {"maxResults": 250, "showDeleted": "true", "singleEvents": "false"}
        if sync_token:
            params["syncToken"] = sync_token
        else:
            now = datetime.now(timezone.utc)
            params["timeMin"] = (now - timedelta(days=_LOOKBACK_DAYS)).isoformat()
            params["timeMax"] = (now + timedelta(days=_LOOKAHEAD_DAYS)).isoformat()
        if page_token:
            params["pageToken"] = page_token
        resp = client.get(
            f"{_API_BASE}/calendars/{quote(gcal_id, safe='')}/events",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        if resp.status_code == 410:
            raise _GoogleGoneError()
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", []):
            action = _apply_event(db, owner, local_cal_id, account_id, item, pending)
            if action == "deleted":
                deleted += 1
            elif action == "processed":
                events_seen += 1
        page_token = data.get("nextPageToken")
        if data.get("nextSyncToken"):
            next_sync_token = data["nextSyncToken"]
        if not page_token:
            break
    db.commit()
    return next_sync_token, events_seen, deleted


def _apply_event(db, owner, local_cal_id, account_id, item: dict, pending: dict):
    from core.database import CalendarEvent

    item_id = item.get("id") or ""
    if not item_id:
        return None
    uid = f"google:{account_id}:{item_id}"
    status = item.get("status") or "confirmed"
    recurring_event_id = item.get("recurringEventId")

    existing = pending.get(uid) or db.query(CalendarEvent).filter(
        CalendarEvent.uid == uid, CalendarEvent.calendar_id == local_cal_id,
    ).first()

    if recurring_event_id and status == "cancelled":
        # A single occurrence was cancelled: exdate the local master series.
        master_uid = f"google:{account_id}:{recurring_event_id}"
        master = pending.get(master_uid) or db.query(CalendarEvent).filter(
            CalendarEvent.uid == master_uid, CalendarEvent.calendar_id == local_cal_id,
        ).first()
        orig = item.get("originalStartTime") or {}
        if master and orig:
            start_dt, all_day, _ = _parse_gtime(orig)
            key = start_dt.strftime("%Y-%m-%d") if (all_day or master.all_day) else start_dt.strftime("%Y-%m-%dT%H:%M")
            exdates = json.loads(master.recurrence_exdates or "[]") if master.recurrence_exdates else []
            if key not in exdates:
                exdates.append(key)
                master.recurrence_exdates = json.dumps(sorted(exdates))
        if existing:
            db.delete(existing)
            pending.pop(uid, None)
            return "deleted"
        return "processed" if master else None

    if status == "cancelled":
        if existing:
            db.delete(existing)
            pending.pop(uid, None)
            return "deleted"
        return None

    start_node = item.get("start") or {}
    end_node = item.get("end") or {}
    if not start_node:
        return None
    start_dt, all_day, start_utc = _parse_gtime(start_node)
    if end_node:
        end_dt, _, end_utc = _parse_gtime(end_node)
    elif all_day:
        end_dt, end_utc = start_dt + timedelta(days=1), False
    else:
        end_dt, end_utc = start_dt + timedelta(hours=1), start_utc
    if end_dt <= start_dt:
        end_dt = start_dt + (timedelta(days=1) if all_day else timedelta(hours=1))

    rrule, exdate_keys = ("", [])
    if not recurring_event_id:
        rrule, exdate_keys = _rrule_and_exdates(item.get("recurrence") or [])

    summary = item.get("summary") or ""
    description = item.get("description") or ""
    location = item.get("location") or ""
    etag = str(item.get("etag") or "").strip('"')
    origin = "google-instance" if recurring_event_id else "google"

    if existing:
        if existing.caldav_sync_pending in {"create", "update"}:
            # A local edit hasn't been pushed yet — don't clobber it with a
            # stale remote read (mirrors caldav_sync's same guard).
            return "processed"
        existing.summary = summary
        existing.description = description
        existing.location = location
        existing.dtstart = start_dt
        existing.dtend = end_dt
        existing.all_day = all_day
        existing.is_utc = start_utc
        existing.rrule = rrule
        if not recurring_event_id:
            existing.recurrence_exdates = json.dumps(sorted(exdate_keys)) if exdate_keys else ""
        existing.origin = origin
        existing.remote_href = item_id
        existing.remote_etag = etag or None
        existing.caldav_sync_pending = None
        return "processed"

    new_ev = CalendarEvent(
        uid=uid, calendar_id=local_cal_id, summary=summary, description=description,
        location=location, dtstart=start_dt, dtend=end_dt, all_day=all_day,
        is_utc=start_utc, rrule=rrule,
        recurrence_exdates=json.dumps(sorted(exdate_keys)) if exdate_keys else "",
        origin=origin, remote_href=item_id, remote_etag=etag or None,
    )
    db.add(new_ev)
    pending[uid] = new_ev
    return "processed"


# ── push (local -> remote) ──────────────────────────────────────────────────

def _event_payload(ev) -> dict:
    return {
        "uid": ev.uid,
        "summary": ev.summary,
        "description": ev.description,
        "location": ev.location,
        "dtstart": ev.dtstart,
        "dtend": ev.dtend,
        "all_day": ev.all_day,
        "is_utc": ev.is_utc,
        "rrule": ev.rrule or "",
        "recurrence_exdates": (
            json.loads(ev.recurrence_exdates or "[]") if getattr(ev, "recurrence_exdates", "") else []
        ),
        "remote_href": getattr(ev, "remote_href", None) or "",
        "remote_etag": getattr(ev, "remote_etag", None) or "",
    }


def _resolve_google_target(owner: str, calendar_id: str):
    """(account_id, google_calendar_id) for a local calendar row, or None if
    it isn't a Google-backed calendar owned by *owner*."""
    from core.database import CalendarCal, SessionLocal

    db = SessionLocal()
    try:
        cal = db.query(CalendarCal).filter(
            CalendarCal.id == calendar_id, CalendarCal.owner == owner,
        ).first()
        if not cal or cal.source != "google" or not cal.account_id or not cal.caldav_base_url:
            return None
        return cal.account_id, cal.caldav_base_url
    finally:
        db.close()


def _load_event_for_writeback(owner: str, uid: str):
    from core.database import CalendarCal, CalendarEvent, SessionLocal

    db = SessionLocal()
    try:
        ev = (
            db.query(CalendarEvent).join(CalendarCal)
            .filter(CalendarEvent.uid == uid, CalendarCal.owner == owner)
            .first()
        )
        if not ev or not ev.calendar or ev.calendar.source != "google":
            return None
        return ev.calendar.source, ev.calendar.id, _event_payload(ev)
    finally:
        db.close()


def _load_delete_for_writeback(owner: str, uid: str):
    from core.database import CalendarCal, CalendarDeletedEvent, CalendarEvent, SessionLocal

    db = SessionLocal()
    try:
        tombstone = db.query(CalendarDeletedEvent).filter(
            CalendarDeletedEvent.uid == uid, CalendarDeletedEvent.owner == owner,
        ).first()
        if tombstone:
            cal = db.query(CalendarCal).filter(CalendarCal.id == tombstone.calendar_id).first()
            if not cal or cal.source != "google":
                return None
            return "google", tombstone.calendar_id, {"uid": uid, "remote_href": tombstone.remote_href or ""}

        ev = (
            db.query(CalendarEvent).join(CalendarCal)
            .filter(CalendarEvent.uid == uid, CalendarCal.owner == owner)
            .first()
        )
        if not ev or not ev.calendar or ev.calendar.source != "google":
            return None
        return ev.calendar.source, ev.calendar.id, {"uid": uid, "remote_href": ev.remote_href or ""}
    finally:
        db.close()


def _pending_writeback_uids(owner: str):
    from core.database import CalendarCal, CalendarDeletedEvent, CalendarEvent, SessionLocal

    db = SessionLocal()
    try:
        rows = (
            db.query(CalendarEvent.uid).join(CalendarCal)
            .filter(
                CalendarCal.owner == owner,
                CalendarCal.source == "google",
                CalendarEvent.status != "cancelled",
                (
                    (CalendarEvent.caldav_sync_pending.isnot(None))
                    | (CalendarEvent.remote_href.is_(None))
                ),
            ).all()
        )
        delete_rows = (
            db.query(CalendarDeletedEvent.uid)
            .join(CalendarCal, CalendarCal.id == CalendarDeletedEvent.calendar_id)
            .filter(CalendarDeletedEvent.owner == owner, CalendarCal.source == "google")
            .all()
        )
        return [r[0] for r in rows], [r[0] for r in delete_rows]
    finally:
        db.close()


def _google_event_body(ev: dict) -> dict:
    body = {
        "summary": ev.get("summary") or "",
        "description": ev.get("description") or "",
        "location": ev.get("location") or "",
    }
    dtstart = ev["dtstart"]
    dtend = ev["dtend"]
    if ev.get("all_day"):
        body["start"] = {"date": dtstart.strftime("%Y-%m-%d")}
        body["end"] = {"date": dtend.strftime("%Y-%m-%d")}
    elif ev.get("is_utc"):
        body["start"] = {"dateTime": dtstart.replace(tzinfo=timezone.utc).isoformat()}
        body["end"] = {"dateTime": dtend.replace(tzinfo=timezone.utc).isoformat()}
    else:
        body["start"] = {"dateTime": dtstart.isoformat()}
        body["end"] = {"dateTime": dtend.isoformat()}
    lines = []
    if ev.get("rrule"):
        lines.append(f"RRULE:{ev['rrule']}")
    for exdate in ev.get("recurrence_exdates") or []:
        lines.append(_exdate_line(str(exdate), bool(ev.get("all_day"))))
    if lines:
        body["recurrence"] = lines
    return body


def _push_blocking(owner: str, calendar_id: str, ev: dict, delete: bool) -> dict:
    target = _resolve_google_target(owner, calendar_id)
    if not target:
        return {"ok": False, "error": "google calendar not found"}
    account_id, gcal_id = target
    from src.google_calendar_accounts import access_token_for

    token = access_token_for(owner, account_id)
    if not token:
        return {"ok": False, "error": "needs_reauth"}

    client = _client()
    try:
        remote_href = str(ev.get("remote_href") or "").strip()
        base = f"{_API_BASE}/calendars/{quote(gcal_id, safe='')}/events"
        state = {"token": token, "retried": False}

        def _call(method, url, **kwargs):
            headers = kwargs.pop("headers", {})
            headers["Authorization"] = f"Bearer {state['token']}"
            resp = client.request(method, url, headers=headers, **kwargs)
            if resp.status_code == 401 and not state["retried"]:
                state["retried"] = True
                refreshed = access_token_for(owner, account_id, force_refresh=True)
                if refreshed:
                    state["token"] = refreshed
                    headers["Authorization"] = f"Bearer {refreshed}"
                    resp = client.request(method, url, headers=headers, **kwargs)
            return resp

        if delete:
            if not remote_href:
                return {"ok": True, "skipped": True}
            resp = _call("DELETE", f"{base}/{quote(remote_href, safe='')}")
            if resp.status_code in (200, 204, 404, 410):
                return {"ok": True}
            if resp.status_code == 401:
                return {"ok": False, "error": "needs_reauth"}
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:150]}"}

        body = _google_event_body(ev)
        known_etag = str(ev.get("remote_etag") or "").strip()
        if remote_href:
            headers = {"Content-Type": "application/json"}
            if known_etag:
                headers["If-Match"] = known_etag if known_etag.startswith('"') else f'"{known_etag}"'
            resp = _call("PATCH", f"{base}/{quote(remote_href, safe='')}", json=body, headers=headers)
            if resp.status_code == 412:
                return {
                    "ok": False, "conflict": True,
                    "reason": "remote changed since the last sync",
                }
            if resp.status_code == 401:
                return {"ok": False, "error": "needs_reauth"}
            if resp.status_code >= 400:
                return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:150]}"}
            data = resp.json()
            return {
                "ok": True, "updated": True,
                "remote_href": data.get("id") or remote_href,
                "remote_etag": str(data.get("etag") or "").strip('"') or None,
            }

        resp = _call("POST", base, json=body, headers={"Content-Type": "application/json"})
        if resp.status_code == 401:
            return {"ok": False, "error": "needs_reauth"}
        if resp.status_code >= 400:
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:150]}"}
        data = resp.json()
        return {
            "ok": True, "created": True,
            "remote_href": data.get("id"),
            "remote_etag": str(data.get("etag") or "").strip('"') or None,
        }
    finally:
        client.close()


def _persist_push_result(owner: str, calendar_id: str, uid: str, result: dict, *, delete: bool) -> None:
    from core.database import CalendarCal, CalendarDeletedEvent, CalendarEvent, SessionLocal

    if not uid or not isinstance(result, dict):
        return
    db = SessionLocal()
    try:
        if delete:
            tombstone = db.query(CalendarDeletedEvent).filter(
                CalendarDeletedEvent.uid == uid, CalendarDeletedEvent.owner == owner,
            ).first()
            if result.get("ok"):
                if tombstone:
                    db.delete(tombstone)
            elif tombstone:
                tombstone.last_error = str(result.get("error") or result)[:500]
            db.commit()
            return

        event = (
            db.query(CalendarEvent).join(CalendarCal)
            .filter(CalendarEvent.uid == uid, CalendarCal.owner == owner)
            .first()
        )
        if event and result.get("ok"):
            if result.get("remote_href"):
                event.remote_href = result.get("remote_href")
            if result.get("remote_etag"):
                event.remote_etag = result.get("remote_etag")
            event.caldav_sync_pending = None
        elif event and result.get("conflict"):
            event.caldav_sync_pending = "conflict"
        elif event and result.get("error") == "needs_reauth":
            # Keep retrying on the next /sync — don't drop the local edit.
            event.caldav_sync_pending = event.caldav_sync_pending or "update"
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Google Calendar push metadata persistence failed")
    finally:
        db.close()


async def push_event_create(owner: str, uid: str) -> dict:
    import asyncio

    loaded = _load_event_for_writeback(owner, uid)
    if not loaded:
        return {"ok": True, "skipped": True}
    _, calendar_id, payload = loaded
    result = await asyncio.to_thread(_push_blocking, owner, calendar_id, payload, False)
    _persist_push_result(owner, calendar_id, uid, result, delete=False)
    return result


async def push_event_update(owner: str, uid: str) -> dict:
    return await push_event_create(owner, uid)


async def push_event_delete(owner: str, uid: str) -> dict:
    import asyncio

    loaded = _load_delete_for_writeback(owner, uid)
    if not loaded:
        return {"ok": True, "skipped": True}
    _, calendar_id, payload = loaded
    result = await asyncio.to_thread(_push_blocking, owner, calendar_id, payload, True)
    _persist_push_result(owner, calendar_id, uid, result, delete=True)
    return result


async def push_pending(owner: str) -> dict:
    result = {"events": 0, "errors": []}
    uids, delete_uids = _pending_writeback_uids(owner)
    for uid in uids:
        try:
            out = await push_event_update(owner, uid)
            if out.get("ok"):
                result["events"] += 1
            elif not out.get("skipped"):
                result["errors"].append(f"{uid}: {str(out.get('error') or out)[:160]}")
        except Exception as e:
            logger.warning("Google Calendar pending push failed for uid=%s: %s", uid, e)
            result["errors"].append(f"{uid}: {str(e)[:160]}")
    for uid in delete_uids:
        try:
            out = await push_event_delete(owner, uid)
            if out.get("ok"):
                result["events"] += 1
            elif not out.get("skipped"):
                result["errors"].append(f"{uid}: {str(out.get('error') or out)[:160]}")
        except Exception as e:
            logger.warning("Google Calendar pending delete failed for uid=%s: %s", uid, e)
            result["errors"].append(f"{uid}: {str(e)[:160]}")
    return result


async def sync_google_direction(owner: str, direction: str = "pull") -> dict:
    direction = (direction or "pull").strip().lower()
    if direction == "pull":
        return await pull(owner)
    if direction == "push":
        return await push_pending(owner)
    if direction == "both":
        pushed = await push_pending(owner)
        pulled = await pull(owner)
        return {"push": pushed, "pull": pulled}
    return {
        "calendars": 0, "events": 0, "deleted": 0,
        "errors": [f"Unsupported Google Calendar sync direction: {direction}"],
    }
