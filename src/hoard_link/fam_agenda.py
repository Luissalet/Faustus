"""The agenda contract, from inside an app: ``GET /api/family/agenda``.

The hub's Today view and the family calendar (``.ics``) ask every running
app what is coming up for the person: deadlines, deliveries, birthdays,
maintenance, releases, exams. An app answers with a *provider*: one
function that returns plain dicts for a date range. This module is the
rest — the route, the bearer-token check against the app's own token file
(the one :mod:`hoard_link.family` configured), the date parsing and the
normalisation that drops what the hub cannot show. It never answers 500:
a provider that raises produces ``{"ok": false, "error": ..., "items": []}``.

::

    from . import fam_agenda

    def provider(date_from, date_to, sphere):          # date, date, str
        return [{"id": "kafka:deadline:41", "title": "Renew the lease",
                 "start": "2026-10-05", "kind": "deadline", "priority": "high",
                 "url": "http://127.0.0.1:5200/#/deadlines/41"}]

    fam_agenda.install_fastapi(app, provider)           # after family.install_fastapi / configure

``sphere`` is ``""`` when the hub wants everything (the hub filters by
sphere itself; an app that knows which items are work and which are
personal may use it to answer less, and may set ``"sphere"`` on an item).

Item keys (the contract in ``docs/FAMILY.md``): ``id`` (stable, ``<app>:<kind>:<n>``),
``title``, ``start`` (``YYYY-MM-DD`` or an ISO date-time, with or without
offset), ``end`` (optional), ``all_day``, ``kind`` (deadline, delivery,
birthday, followup, maintenance, release, review, cards, incident, publish,
renewal, exam, other), ``priority`` (low, normal, high, urgent), ``url``,
``detail``, ``sphere``. A provider may return ``date`` / ``datetime``
objects for ``start`` and ``end``.

Standard library only; ``fastapi`` is imported only to install the route.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from . import family as _family

try:  # only for install_fastapi
    from fastapi import Request as _Request
    from fastapi.responses import JSONResponse as _JSONResponse
except Exception:  # noqa: BLE001
    _Request = Any  # type: ignore[misc,assignment]
    _JSONResponse = None  # type: ignore[assignment]

AGENDA_PATH = "/api/family/agenda"
KINDS = ("deadline", "delivery", "birthday", "followup", "maintenance", "release", "review", "cards",
         "incident", "publish", "renewal", "exam", "other")
PRIORITIES = ("low", "normal", "high", "urgent")
DEFAULT_PAST_DAYS = 7
DEFAULT_FUTURE_DAYS = 60
MAX_SPAN_DAYS = 400
MAX_ITEMS = 500
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_URL_OK = re.compile(r"^(https?|hoard)://\S+$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------

def parse_when(value: Any) -> Optional[tuple[date, Optional[datetime]]]:
    """``value`` (a date, a datetime or an ISO string) → ``(day, moment)``. ``moment`` is None for a bare
    date; it keeps its offset when it has one. None when it is not a date at all."""
    if isinstance(value, datetime):
        return value.date(), value
    if isinstance(value, date):
        return value, None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if _DATE_ONLY.match(text):
        try:
            return date.fromisoformat(text), None
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    return dt.date(), dt


def parse_range(date_from: Any = None, date_to: Any = None, *, today: Optional[date] = None) -> tuple[date, date]:
    """The window an agenda request asks for. Defaults: today-7 … today+60; a bad value falls back to its
    default; an inverted range is swapped; the span is capped at ``MAX_SPAN_DAYS``."""
    today = today or date.today()
    a = parse_when(date_from)
    b = parse_when(date_to)
    start = a[0] if a else today - timedelta(days=DEFAULT_PAST_DAYS)
    end = b[0] if b else today + timedelta(days=DEFAULT_FUTURE_DAYS)
    if end < start:
        start, end = end, start
    if (end - start).days > MAX_SPAN_DAYS:
        end = start + timedelta(days=MAX_SPAN_DAYS)
    return start, end


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------

def _clip(value: Any, n: int) -> str:
    return " ".join(str(value if value is not None else "").split())[:n]


def normalize_item(raw: Any, *, app: str = "", default_sphere: str = "") -> Optional[dict[str, Any]]:
    """One provider item → the contract's shape, or None when it cannot be shown (no title, no usable start)."""
    if not isinstance(raw, dict):
        return None
    title = _clip(raw.get("title"), 200)
    start = parse_when(raw.get("start"))
    if not title or start is None:
        return None
    day, moment = start
    all_day = raw.get("all_day") if isinstance(raw.get("all_day"), bool) else moment is None
    if moment is None:
        all_day = True
    out_start = day.isoformat() if all_day else moment.isoformat(timespec="seconds")  # type: ignore[union-attr]
    end_out: Optional[str] = None
    end = parse_when(raw.get("end"))
    if end is not None:
        eday, emoment = end
        if all_day:
            if eday >= day:
                end_out = eday.isoformat()
        elif emoment is not None and moment is not None:
            try:
                later = emoment >= moment
            except TypeError:           # one naive, one aware: cannot be compared, keep the day check
                later = eday >= day
            if later:
                end_out = emoment.isoformat(timespec="seconds")
    kind = str(raw.get("kind") or "").strip().lower()
    kind = kind if kind in KINDS else "other"
    priority = str(raw.get("priority") or "").strip().lower()
    priority = priority if priority in PRIORITIES else "normal"
    url = str(raw.get("url") or "").strip()
    url = url[:500] if _URL_OK.match(url) else ""
    item_id = _clip(raw.get("id"), 160).replace(" ", "_")
    if not item_id:
        digest = hashlib.sha1(f"{title}|{out_start}".encode("utf-8")).hexdigest()[:10]
        item_id = f"{app or 'app'}:{kind}:{digest}"
    elif ":" not in item_id and app:
        item_id = f"{app}:{item_id}"
    sphere = _clip(raw.get("sphere"), 40).lower() or default_sphere
    item: dict[str, Any] = {"id": item_id, "title": title, "start": out_start, "all_day": bool(all_day),
                            "kind": kind, "priority": priority, "url": url,
                            "detail": _clip(raw.get("detail"), 300), "sphere": sphere}
    if end_out:
        item["end"] = end_out
    dedupe_key = _clip(raw.get("dedupe_key"), 200)
    if dedupe_key:
        item["dedupe_key"] = dedupe_key
    return item


def normalize_items(result: Any, *, app: str = "", default_sphere: str = "") -> list[dict[str, Any]]:
    """A provider's return value (a list, or ``{"items": [...]}``) → clean items, capped at ``MAX_ITEMS``."""
    if isinstance(result, dict):
        result = result.get("items")
    if not isinstance(result, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in result:
        item = normalize_item(raw, app=app, default_sphere=default_sphere)
        if item is None or item["id"] in seen:
            continue
        seen.add(item["id"])
        out.append(item)
        if len(out) >= MAX_ITEMS:
            break
    return out


def _app_id() -> str:
    try:
        return str(_family.status().get("app") or "")
    except Exception:  # noqa: BLE001
        return ""


def build_response(result: Any, date_from: date, date_to: date, sphere: str) -> dict[str, Any]:
    return {"ok": True, "items": normalize_items(result, app=_app_id(), default_sphere=""),
            "from": date_from.isoformat(), "to": date_to.isoformat(), "sphere": sphere}


def error_response(exc: BaseException, date_from: date, date_to: date, sphere: str) -> dict[str, Any]:
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300], "items": [],
            "from": date_from.isoformat(), "to": date_to.isoformat(), "sphere": sphere}


def answer(provider: Callable[..., Any], date_from: Any = None, date_to: Any = None, sphere: str = "", *,
           today: Optional[date] = None) -> dict[str, Any]:
    """Run a *synchronous* provider for a request and return the response body. Never raises."""
    start, end = parse_range(date_from, date_to, today=today)
    sphere = _clip(sphere, 40).lower()
    try:
        return build_response(provider(start, end, sphere), start, end, sphere)
    except Exception as exc:  # noqa: BLE001
        return error_response(exc, start, end, sphere)


async def answer_async(provider: Callable[..., Any], date_from: Any = None, date_to: Any = None, sphere: str = "", *,
                       today: Optional[date] = None) -> dict[str, Any]:
    """Like :func:`answer` for a provider that is a coroutine function (awaited) or a plain function
    (run in a worker thread so it cannot block the event loop)."""
    start, end = parse_range(date_from, date_to, today=today)
    sphere = _clip(sphere, 40).lower()
    try:
        if inspect.iscoroutinefunction(provider):
            result = await provider(start, end, sphere)
        else:
            result = await asyncio.to_thread(provider, start, end, sphere)
            if inspect.isawaitable(result):
                result = await result
        return build_response(result, start, end, sphere)
    except Exception as exc:  # noqa: BLE001
        return error_response(exc, start, end, sphere)


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

def _bearer(headers: Any) -> str:
    header = headers.get("authorization", "") if headers is not None else ""
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def token_ok(given: str, token_file: Optional[str] = None) -> bool:
    """True when ``given`` is the app's own token (read from its token file at call time: a rotated token works)."""
    path = token_file or str(_family.status().get("token_file") or "")
    if not path or not given:
        return False
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            expected = fh.read().strip()
    except OSError:
        return False
    return bool(expected) and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def install_fastapi(app: Any, provider: Callable[..., Any], *, path: str = AGENDA_PATH,
                    token_file: Optional[str] = None) -> dict[str, Any]:
    """Add ``GET /api/family/agenda`` to a FastAPI app. The caller must send the app's own bearer token
    (``family.status()["token_file"]`` unless ``token_file`` says otherwise). Safe to call before or after
    :func:`hoard_link.family.install_fastapi`; the route is moved ahead of any catch-all the app registered.
    Returns ``{"installed": bool, "path": ...}``."""
    if _JSONResponse is None:
        return {"installed": False, "path": path, "error": "fastapi is not installed"}
    JSONResponse = _JSONResponse

    async def agenda_route(request: _Request) -> Any:
        if not token_ok(_bearer(request.headers), token_file):
            return JSONResponse({"ok": False, "error": "a family bearer token is required", "items": []}, status_code=401)
        q = request.query_params
        body = await answer_async(provider, q.get("from"), q.get("to"), q.get("sphere") or "")
        return JSONResponse(body, status_code=200)

    app.add_api_route(path, agenda_route, methods=["GET"], include_in_schema=False)
    routes = getattr(getattr(app, "router", None), "routes", None)
    if isinstance(routes, list) and len(routes) >= 2:
        ours = routes.pop()
        routes.insert(0, ours)
    return {"installed": True, "path": path}


__all__ = ["AGENDA_PATH", "KINDS", "PRIORITIES", "parse_when", "parse_range", "normalize_item", "normalize_items",
           "answer", "answer_async", "token_ok", "install_fastapi"]
