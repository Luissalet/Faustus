"""iCalendar (RFC 5545) export and a small parser. Standard library only.

The Node twin is ``js/hoard-commons/ics.js`` (same names in camelCase); both are checked against
``tests/vectors/ics.json``. Replaces the three hand-rolled exporters (Faustus calendar routes, Phileas ``travel/ics.py``,
People ``calendar.js``).

:func:`build_ics` takes plain event dicts (the keys below; unknown keys are ignored):

=================  ============================================================================================
``uid`` / ``id``   stable id. Without ``@`` it gets ``@hoard`` appended; missing -> sha1 of title, start, end, place.
``title``          also ``summary``.
``start``          ``date``, ``datetime`` or ISO text: ``2026-10-02`` (all day), ``2026-10-02T10:00`` (local time),
                   ``2026-10-02T10:00:00Z`` / ``+02:00`` (an instant, written in UTC). ``20261002T100000Z`` works too.
``end``            same forms. For an all-day event it is the **last day** (inclusive) unless ``end_exclusive`` is true;
                   missing = one day. A timed event without ``end`` has no DTEND.
``all_day``        force all-day (the time of ``start`` is dropped).
``tz``             (or ``tzid``) zone of local times, written as ``TZID=`` (no VTIMEZONE block is emitted). Falls back to
                   the calendar's ``tz``; without any, local times are "floating".
``description``    also ``detail``, ``notes``.   ``location``, ``url``, ``categories`` (list or text), ``rrule`` (a leading
                   ``RRULE:`` is dropped), ``status`` (confirmed / tentative / cancelled), ``priority`` (0-9),
                   ``transp`` (opaque / transparent).
``alarms``         list of minutes before the start (``15``; negative = after), ``{"minutes": 15}`` or
                   ``{"at": "2026-10-02T08:00:00Z"}`` (absolute); optional ``text``.
=================  ============================================================================================

:func:`parse_ics` is the reverse for the same shape (see its docstring) and is enough to read what these exporters and
common calendar apps write; it is not a validator.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

__all__ = ["ics_escape", "ics_unescape", "fold_line", "build_ics", "parse_ics"]

DEFAULT_PRODID = "-//Hoard//Family//EN"


def ics_escape(text: Any) -> str:
    """Escape a TEXT value: backslash, semicolon, comma and line breaks."""
    s = "" if text is None else str(text)
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


_UNESC = re.compile(r"\\([nN,;\\])")


def ics_unescape(text: Any) -> str:
    return _UNESC.sub(lambda m: "\n" if m.group(1) in "nN" else m.group(1), "" if text is None else str(text))


def fold_line(line: str, limit: int = 75) -> str:
    """Fold a content line at ``limit`` octets (UTF-8 safe: a character is never split); continuation lines start with
    one space, so they carry ``limit - 1`` octets. Returns the pieces joined with CRLF + space (no trailing CRLF)."""
    if len(line.encode("utf-8")) <= limit:
        return line
    out: list[str] = []
    cur, size = "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > (limit if not out else limit - 1):
            out.append(cur)
            cur, size = ch, n
        else:
            cur += ch
            size += n
    out.append(cur)
    return "\r\n ".join(out)


# ------------------------------------------------------------------------------------------------ time values
_WHEN = re.compile(
    r"^\s*([0-9]{4})-?([0-9]{2})-?([0-9]{2})(?:[T ]([0-9]{2}):?([0-9]{2})(?::?([0-9]{2}))?(?:[.,][0-9]+)?)?\s*(Z|[+-][0-9]{2}(?::?[0-9]{2})?)?\s*$",
    re.I,
)


def _when(value: Any) -> Optional[dict[str, Any]]:
    """``{"k": "date"|"local"|"utc", "y", "mo", "d", "h", "mi", "s"}`` or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
            k = "utc"
        else:
            k = "local"
        return {"k": k, "y": value.year, "mo": value.month, "d": value.day, "h": value.hour, "mi": value.minute, "s": value.second}
    if isinstance(value, date):
        return {"k": "date", "y": value.year, "mo": value.month, "d": value.day, "h": 0, "mi": 0, "s": 0}
    m = _WHEN.match(str(value))
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        date(y, mo, d)
    except ValueError:
        return None
    if m.group(4) is None:
        return {"k": "date", "y": y, "mo": mo, "d": d, "h": 0, "mi": 0, "s": 0}
    h, mi, s = int(m.group(4)), int(m.group(5)), int(m.group(6) or 0)
    if h > 23 or mi > 59 or s > 59:
        return None
    zone = m.group(7)
    if not zone:
        return {"k": "local", "y": y, "mo": mo, "d": d, "h": h, "mi": mi, "s": s}
    dt = datetime(y, mo, d, h, mi, s)
    if zone.upper() != "Z":
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        dt -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
    return {"k": "utc", "y": dt.year, "mo": dt.month, "d": dt.day, "h": dt.hour, "mi": dt.minute, "s": dt.second}


def _add(w: dict[str, Any], *, days: int = 0, seconds: int = 0) -> dict[str, Any]:
    if w["k"] == "date":
        t = date(w["y"], w["mo"], w["d"]) + timedelta(days=days + seconds // 86400)
        return {**w, "y": t.year, "mo": t.month, "d": t.day}
    t = datetime(w["y"], w["mo"], w["d"], w["h"], w["mi"], w["s"]) + timedelta(days=days, seconds=seconds)
    return {**w, "y": t.year, "mo": t.month, "d": t.day, "h": t.hour, "mi": t.minute, "s": t.second}


def _ymd(w: dict[str, Any]) -> str:
    return f"{w['y']:04d}{w['mo']:02d}{w['d']:02d}"


def _compact(w: dict[str, Any]) -> str:
    return f"{_ymd(w)}T{w['h']:02d}{w['mi']:02d}{w['s']:02d}" + ("Z" if w["k"] == "utc" else "")


def _iso(w: dict[str, Any]) -> str:
    day = f"{w['y']:04d}-{w['mo']:02d}-{w['d']:02d}"
    if w["k"] == "date":
        return day
    return f"{day}T{w['h']:02d}:{w['mi']:02d}:{w['s']:02d}" + ("Z" if w["k"] == "utc" else "")


def _stamp(now: Any) -> str:
    w = _when(now) if now is not None else None
    if w is None:
        w = _when(datetime.now(timezone.utc))
    return _compact({**w, "k": "utc"})


def _text(ev: dict[str, Any], *names: str) -> str:
    for n in names:
        v = ev.get(n)
        if v is not None and str(v).strip() != "":
            return str(v)
    return ""


def _trigger(minutes: int) -> str:
    return f"{'-' if minutes > 0 else ''}PT{abs(int(minutes))}M"


def _alarms(ev: dict[str, Any], title: str) -> list[str]:
    out: list[str] = []
    raw = ev.get("alarms")
    if raw is None or isinstance(raw, (str, bytes)):
        raw = [] if raw is None else [raw]
    elif isinstance(raw, (int, float)):
        raw = [raw]
    for a in raw:
        text = title or "Reminder"
        if isinstance(a, dict):
            text = _text(a, "text", "description") or text
            at = _when(a.get("at")) if a.get("at") else None
            if at and at["k"] != "date":
                trig = "TRIGGER;VALUE=DATE-TIME:" + _compact({**at, "k": "utc"})
            elif a.get("minutes") is not None:
                trig = "TRIGGER:" + _trigger(int(a["minutes"]))
            else:
                continue
        else:
            try:
                trig = "TRIGGER:" + _trigger(int(a))
            except (TypeError, ValueError):
                continue
        out += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{ics_escape(text)}", trig, "END:VALARM"]
    return out


def _event_lines(ev: dict[str, Any], stamp: str, cal_tz: str) -> list[str]:
    title = _text(ev, "title", "summary")
    start = _when(ev.get("start") if ev.get("start") not in (None, "") else ev.get("dtstart"))
    if start is None:
        return []
    end = _when(ev.get("end") if ev.get("end") not in (None, "") else ev.get("dtend"))
    all_day = bool(ev.get("all_day") or ev.get("allDay")) or start["k"] == "date"
    tz = _text(ev, "tz", "tzid") or cal_tz
    lines = ["BEGIN:VEVENT"]
    if all_day:
        start = {**start, "k": "date"}
        if end is None:
            last_excl = _add(start, days=1)
        else:
            end = {**end, "k": "date"}
            last_excl = end if (ev.get("end_exclusive") or ev.get("endExclusive")) else _add(end, days=1)
            if (last_excl["y"], last_excl["mo"], last_excl["d"]) <= (start["y"], start["mo"], start["d"]):
                last_excl = _add(start, days=1)
        dtstart, dtend = f"DTSTART;VALUE=DATE:{_ymd(start)}", f"DTEND;VALUE=DATE:{_ymd(last_excl)}"
        key = f"{_ymd(start)}|{_ymd(last_excl)}"
    else:
        head = f";TZID={tz}" if (start["k"] == "local" and tz) else ""
        dtstart = f"DTSTART{head}:{_compact(start)}"
        dtend = ""
        key = _compact(start) + "|"
        if end is not None:
            if end["k"] == "date":
                end = {**end, "k": start["k"]}
            ehead = f";TZID={tz}" if (end["k"] == "local" and tz) else ""
            dtend = f"DTEND{ehead}:{_compact(end)}"
            key += _compact(end)
    uid = _text(ev, "uid", "id").strip()
    if not uid:
        uid = hashlib.sha1("\x1f".join([title, key, _text(ev, "location")]).encode("utf-8")).hexdigest()[:20]
    if "@" not in uid:
        uid += "@hoard"
    lines += [f"UID:{uid}", f"DTSTAMP:{stamp}", dtstart]
    if dtend:
        lines.append(dtend)
    rrule = _text(ev, "rrule").strip()
    if rrule:
        lines.append("RRULE:" + re.sub(r"^RRULE:", "", rrule, flags=re.I))
    lines.append(f"SUMMARY:{ics_escape(title)}")
    desc = _text(ev, "description", "detail", "notes")
    if desc:
        lines.append(f"DESCRIPTION:{ics_escape(desc)}")
    loc = _text(ev, "location")
    if loc:
        lines.append(f"LOCATION:{ics_escape(loc)}")
    url = _text(ev, "url").replace("\r", "").replace("\n", "")
    if url:
        lines.append(f"URL:{url}")
    cats = ev.get("categories")
    if isinstance(cats, str):
        cats = [c.strip() for c in cats.split(",")]
    cats = [str(c) for c in (cats or []) if str(c).strip()]
    if cats:
        lines.append("CATEGORIES:" + ",".join(ics_escape(c) for c in cats))
    status = _text(ev, "status").upper()
    if status in ("CONFIRMED", "TENTATIVE", "CANCELLED"):
        lines.append(f"STATUS:{status}")
    if ev.get("priority") is not None and str(ev["priority"]).strip().lstrip("-").isdigit() and 0 <= int(ev["priority"]) <= 9:
        lines.append(f"PRIORITY:{int(ev['priority'])}")
    transp = _text(ev, "transp").upper()
    if transp in ("OPAQUE", "TRANSPARENT"):
        lines.append(f"TRANSP:{transp}")
    lines += _alarms(ev, title)
    lines.append("END:VEVENT")
    return lines


def build_ics(events: Iterable[dict[str, Any]], *, name: Optional[str] = None, prodid: str = DEFAULT_PRODID,
              tz: Optional[str] = None, now: Any = None) -> str:
    """The calendar text (CRLF line ends, folded at 75 octets, ending with a CRLF). ``name`` -> ``X-WR-CALNAME``,
    ``tz`` -> ``X-WR-TIMEZONE`` and the ``TZID`` of local times, ``now`` (ISO text, ``datetime``) -> every DTSTAMP
    (default: the current UTC time; pass it for reproducible output). Events without a usable ``start`` are skipped."""
    stamp = _stamp(now)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{prodid}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    if name:
        lines.append(f"X-WR-CALNAME:{ics_escape(name)}")
    if tz:
        lines.append(f"X-WR-TIMEZONE:{tz}")
    for ev in events:
        lines += _event_lines(ev, stamp, tz or "")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_line(x) for x in lines) + "\r\n"


# ------------------------------------------------------------------------------------------------ parsing
_DURATION = re.compile(r"^([+-])?P(?:([0-9]+)W)?(?:([0-9]+)D)?(?:T(?:([0-9]+)H)?(?:([0-9]+)M)?(?:([0-9]+)S)?)?$", re.I)


def _duration_seconds(value: str) -> Optional[int]:
    m = _DURATION.match(value.strip())
    if not m or not any(m.group(i) for i in range(2, 7)):
        return None
    w, d, h, mi, s = (int(m.group(i) or 0) for i in range(2, 7))
    total = ((w * 7 + d) * 24 + h) * 3600 + mi * 60 + s
    return -total if m.group(1) == "-" else total


def _unfold(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        elif raw != "":
            out.append(raw)
    return out


def _split_line(line: str) -> tuple[str, dict[str, str], str]:
    """``NAME;P=v;P2="x:y":value`` -> (NAME, {P: v}, value); the colon inside quotes does not end the parameters."""
    in_q = False
    cut = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            cut = i
            break
    if cut < 0:
        return line.upper(), {}, ""
    head, value = line[:cut], line[cut + 1:]
    parts = re.split(r';(?=(?:[^"]*"[^"]*")*[^"]*$)', head)
    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _parse_when_prop(value: str, params: dict[str, str]) -> Optional[dict[str, Any]]:
    w = _when(value.strip())
    if w is None:
        return None
    if params.get("VALUE", "").upper() == "DATE":
        w = {**w, "k": "date"}
    return w


def _parse_alarm(props: list[tuple[str, dict[str, str], str]]) -> Any:
    for name, params, value in props:
        if name != "TRIGGER":
            continue
        if params.get("VALUE", "").upper() == "DATE-TIME":
            w = _when(value.strip())
            return {"at": _iso({**w, "k": "utc"})} if w else None
        sec = _duration_seconds(value)
        if sec is not None:
            return -(sec // 60)
    return None


def parse_ics(text: str) -> list[dict[str, Any]]:
    """The VEVENTs of an iCalendar text as dicts with the fixed keys ``uid, summary, description, location, start, end,
    all_day, end_exclusive, tzid, rrule, url, status, categories, alarms``.

    ``start`` / ``end`` are ISO text: ``2026-10-02`` (VALUE=DATE), ``2026-10-02T10:00:00`` (local time, the zone name
    in ``tzid``, which is kept as written) or ``2026-10-02T10:00:00Z``. ``end`` is the raw DTEND (so ``end_exclusive``
    is true for all-day events, which feeds straight back into :func:`build_ics`); a DURATION is turned into an
    ``end``; no end -> ``""``. Text values are unescaped, ``categories`` is a list, ``alarms`` lists minutes before the
    start (negative = after) or ``{"at": ISO}``. Missing text fields are ``""``. Other components are ignored."""
    events: list[dict[str, Any]] = []
    stack: list[str] = []
    cur: Optional[list[tuple[str, dict[str, str], str]]] = None
    alarm: Optional[list[tuple[str, dict[str, str], str]]] = None
    alarms: list[Any] = []
    for line in _unfold(text or ""):
        name, params, value = _split_line(line)
        if name == "BEGIN":
            comp = value.strip().upper()
            stack.append(comp)
            if comp == "VEVENT":
                cur, alarms = [], []
            elif comp == "VALARM" and cur is not None:
                alarm = []
            continue
        if name == "END":
            comp = value.strip().upper()
            if stack and stack[-1] == comp:
                stack.pop()
            if comp == "VALARM" and alarm is not None and cur is not None:
                a = _parse_alarm(alarm)
                if a is not None:
                    alarms.append(a)
                alarm = None
            elif comp == "VEVENT" and cur is not None:
                events.append(_event_from(cur, alarms))
                cur = None
            continue
        if alarm is not None:
            alarm.append((name, params, value))
        elif cur is not None and stack and stack[-1] == "VEVENT":
            cur.append((name, params, value))
    return events


def _event_from(props: list[tuple[str, dict[str, str], str]], alarms: list[Any]) -> dict[str, Any]:
    first: dict[str, tuple[dict[str, str], str]] = {}
    cats: list[str] = []
    for name, params, value in props:
        if name == "CATEGORIES":
            cats += [ics_unescape(c).strip() for c in re.split(r"(?<!\\),", value) if c.strip()]
        elif name not in first:
            first[name] = (params, value)

    def txt(n: str) -> str:
        return ics_unescape(first[n][1]).strip() if n in first else ""

    start = _parse_when_prop(first["DTSTART"][1], first["DTSTART"][0]) if "DTSTART" in first else None
    end = _parse_when_prop(first["DTEND"][1], first["DTEND"][0]) if "DTEND" in first else None
    all_day = bool(start and start["k"] == "date")
    if end is None and start is not None and "DURATION" in first:
        sec = _duration_seconds(first["DURATION"][1])
        if sec is not None:
            end = _add(start, seconds=sec)
    tzid = first["DTSTART"][0].get("TZID", "") if "DTSTART" in first else ""
    return {
        "uid": txt("UID"), "summary": txt("SUMMARY"), "description": txt("DESCRIPTION"), "location": txt("LOCATION"),
        "start": _iso(start) if start else "", "end": _iso(end) if end else "", "all_day": all_day,
        "end_exclusive": all_day, "tzid": tzid, "rrule": first["RRULE"][1].strip() if "RRULE" in first else "",
        "url": first["URL"][1].strip() if "URL" in first else "", "status": txt("STATUS").upper(), "categories": cats,
        "alarms": list(alarms),
    }
