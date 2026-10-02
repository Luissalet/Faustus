"""Dates in the languages the family reads: month and weekday names, dates in text with the role they play, years
that the text leaves out, and Spanish or English relative phrases (``"el viernes"``, ``"en 3 días"``).

Standard library only. The Node twin is ``js/hoard-commons/dates.js`` (same names in camelCase; dates travel as
``"YYYY-MM-DD"`` strings there) and both are checked against ``tests/vectors/dates.json``. Durations (``"2h"``,
``"hace 3 días"``) live in :mod:`hoard_link.since`; business days in :mod:`hoard_link.bizdays`.

* :data:`MONTHS` / :data:`WEEKDAYS` — accent-free aliases per language (es, en, fr, pt, it, de), including the
  spellings that used to differ between apps (``sep``, ``sept``, ``set``, ``setiembre``, with or without a trailing
  dot). :func:`month_number` and :func:`weekday_number` look a word up in every language at once.
* :func:`parse_date` — the first date in a text. :func:`find_dates` — every date with its offsets and a *role*
  taken from the label in front of it (``due``, ``issued``, ``expires``, ``delivery``, ``departure``, ``return``,
  ``purchase``, ``renewal``). A date without a year (``"15 sept"``, ``"15/10"``) gets the year that fits the role and
  ``today``: future for deadlines, past for issue dates, nearest otherwise.
* :func:`parse_due` — ``"mañana"``, ``"el viernes"``, ``"en dos semanas"``, ``"fin de mes"``, ``"15 de octubre"``
  relative to ``today``; None when the words do not name one day (unless ``vague=True``).
* :func:`add_months` (clamps to the end of a short month), :func:`days_between`, :func:`iso_day`.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional, Union

from .text import fold

__all__ = [
    "MONTHS", "MONTH_NAMES", "WEEKDAYS", "WEEKDAY_NAMES", "ROLES", "DateHit", "month_number", "weekday_number",
    "parse_date", "find_dates", "parse_due", "resolve_year", "add_months", "add_days", "days_between", "iso_day",
    "parse_iso", "next_weekday",
]

DateLike = Union[date, datetime, str]

# ---------------------------------------------------------------------------------------------- names
# accent-free aliases per language, January first; a trailing dot is accepted when parsing ("sept.", "oct.")
MONTHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "es": (("enero", "ene"), ("febrero", "feb"), ("marzo", "mar"), ("abril", "abr"), ("mayo", "may"), ("junio", "jun"),
           ("julio", "jul"), ("agosto", "ago"), ("septiembre", "setiembre", "sept", "sep", "set"), ("octubre", "oct"),
           ("noviembre", "nov"), ("diciembre", "dic")),
    "en": (("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
           ("july", "jul"), ("august", "aug"), ("september", "sept", "sep"), ("october", "oct"), ("november", "nov"),
           ("december", "dec")),
    "fr": (("janvier", "janv"), ("fevrier", "fevr"), ("mars",), ("avril", "avr"), ("mai",), ("juin",), ("juillet", "juil"),
           ("aout",), ("septembre", "sept"), ("octobre",), ("novembre",), ("decembre",)),
    "pt": (("janeiro",), ("fevereiro", "fev"), ("marco",), ("abril",), ("maio",), ("junho",), ("julho",), ("agosto",),
           ("setembro", "set"), ("outubro", "out"), ("novembro",), ("dezembro", "dez")),
    "it": (("gennaio", "gen"), ("febbraio",), ("marzo",), ("aprile",), ("maggio", "mag"), ("giugno", "giu"), ("luglio", "lug"),
           ("agosto",), ("settembre", "set"), ("ottobre", "ott"), ("novembre",), ("dicembre", "dic")),
    "de": (("januar",), ("februar",), ("marz", "mrz"), ("april",), ("mai",), ("juni",), ("juli",), ("august",),
           ("september",), ("oktober", "okt"), ("november",), ("dezember", "dez")),
}
MONTH_NAMES: dict[str, tuple[str, ...]] = {
    "es": ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"),
    "en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"),
    "fr": ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"),
    "pt": ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"),
    "it": ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"),
    "de": ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"),
}
# Monday first
WEEKDAYS: dict[str, tuple[tuple[str, ...], ...]] = {
    "es": (("lunes", "lun"), ("martes", "mar"), ("miercoles", "mie"), ("jueves", "jue"), ("viernes", "vie"), ("sabado", "sab"),
           ("domingo", "dom")),
    "en": (("monday", "mon"), ("tuesday", "tue"), ("wednesday", "wed"), ("thursday", "thu"), ("friday", "fri"),
           ("saturday", "sat"), ("sunday", "sun")),
    "fr": (("lundi",), ("mardi",), ("mercredi",), ("jeudi",), ("vendredi",), ("samedi",), ("dimanche",)),
    "pt": (("segunda", "segunda-feira"), ("terca", "terca-feira"), ("quarta", "quarta-feira"), ("quinta", "quinta-feira"),
           ("sexta", "sexta-feira"), ("sabado",), ("domingo",)),
    "it": (("lunedi",), ("martedi",), ("mercoledi",), ("giovedi",), ("venerdi",), ("sabato",), ("domenica",)),
    "de": (("montag",), ("dienstag",), ("mittwoch",), ("donnerstag",), ("freitag",), ("samstag", "sonnabend"), ("sonntag",)),
}
WEEKDAY_NAMES: dict[str, tuple[str, ...]] = {
    "es": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "fr": ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"),
    "pt": ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"),
    "it": ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"),
    "de": ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
}

_MONTH_LOOKUP: dict[str, int] = {}
for _langs in MONTHS.values():
    for _i, _aliases in enumerate(_langs, 1):
        for _a in _aliases:
            _MONTH_LOOKUP[_a] = _i
_WEEKDAY_LOOKUP: dict[str, int] = {}
for _langs in WEEKDAYS.values():
    for _i, _aliases in enumerate(_langs):
        for _a in _aliases:
            _WEEKDAY_LOOKUP[_a] = _i


def month_number(word: Any) -> Optional[int]:
    """1-12 for a month name or abbreviation in any language (``"Sept."``, ``"setiembre"``, ``"März"``), else None."""
    key = fold(word, keep_length=False).strip().rstrip(".") if word is not None else ""
    return _MONTH_LOOKUP.get(key)


def weekday_number(word: Any) -> Optional[int]:
    """0-6 (Monday first) for a weekday name or abbreviation in any language, else None."""
    key = fold(word, keep_length=False).strip().rstrip(".") if word is not None else ""
    return _WEEKDAY_LOOKUP.get(key)


_MONTH_ALT = "|".join(sorted(_MONTH_LOOKUP, key=lambda a: (-len(a), a)))
_WEEKDAY_FULL = "|".join(sorted((a for a in _WEEKDAY_LOOKUP if len(a) >= 5), key=lambda a: (-len(a), a)))

# ---------------------------------------------------------------------------------------------- scanning
_RX_ISO = re.compile(r"(?<![0-9/.-])((?:19|20)[0-9]{2})([-/.])([0-9]{1,2})\2([0-9]{1,2})(?![0-9])")
_RX_NUM = re.compile(r"(?<![0-9/.-])([0-9]{1,2})([/.-])([0-9]{1,2})\2([0-9]{4}|[0-9]{2})(?![0-9]|[./-][0-9])")
_RX_DMY = re.compile(r"(?<![0-9])([0-9]{1,2})(?:st|nd|rd|th|o|º|°)?\.?\s*(?:de\s+|of\s+)?(" + _MONTH_ALT + r")(?![a-z])\.?"
                     r"(?:,?\s*(?:del?\s+|of\s+)?([0-9]{4})(?![0-9]))?")
_RX_MDY = re.compile(r"(?<![a-z0-9])(" + _MONTH_ALT + r")(?![a-z])\.?\s+([0-9]{1,2})(?:st|nd|rd|th)?(?![0-9a-z])"
                     r"(?:,?\s*([0-9]{4})(?![0-9]))?")
_RX_DM = re.compile(r"(?<![0-9/.-])([0-9]{1,2})/([0-9]{1,2})(?![0-9/])")

ROLES = ("due", "expires", "issued", "delivery", "departure", "return", "purchase", "renewal")

_ROLE_SRC: list[tuple[str, str]] = [
    ("due", r"fecha\s+(?:de\s+|limite\s+de\s+)?(?:vencimiento|pago|cargo|cobro|adeudo|domiciliacion)|fecha\s+limite|vencimiento|"
            r"vence(?:\s+el)?|pagar\s+antes\s+del?|pago\s+antes\s+del?|antes\s+del|"
            r"se\s+(?:cargara|cobrara|adeudara|pasara\s+al\s+cobro)(?:\s+(?:en\s+su\s+cuenta\s+)?el)?|"
            r"proximo\s+(?:cobro|cargo|pago|recibo)|siguiente\s+(?:cobro|cargo|pago|recibo)|due\s+date|payment\s+due|due\s+on|"
            r"pay\s+by|next\s+(?:billing(?:\s+date)?|payment|charge)|billing\s+date|(?:pagar|pago|abonar)\s+hasta(?:\s+el)?"),
    ("expires", r"valid[oa]\s+hasta|vigente\s+hasta|fecha\s+de\s+(?:caducidad|expiracion|validez)|caducidad|caduca(?:\s+el)?|"
                r"validez|valid\s+until|valid\s+through|expires?(?:\s+on)?|expiry(?:\s+date)?|expiration(?:\s+date)?|use\s+by|"
                r"best\s+before|consumir\s+preferentemente\s+antes\s+del?|hasta\s+el"),
    ("issued", r"fecha\s+(?:de\s+)?(?:la\s+)?(?:factura|emision|expedicion|edicion|documento|recibo)|fecha\s+factura|"
               r"invoice\s+date|date\s+of\s+issue|issue\s+date|issued(?:\s+on)?|emitid[oa](?:\s+el)?|expedid[oa](?:\s+el)?|fecha|date"),
    ("delivery", r"fecha\s+(?:estimada\s+|prevista\s+)?de\s+entrega|entrega\s+(?:estimada|prevista)|entrega\s+entre|"
                 r"entregad[oa]\s+el|se\s+entregara(?:\s+el)?|llegara(?:\s+el)?|llega(?:\s+el)?|llegada\s+estimada|"
                 r"recibiras(?:\s+el)?|recibelo(?:\s+el)?|delivered(?:\s+on)?|delivery\s+date|(?:estimated|expected)\s+delivery|"
                 r"arrives?(?:\s+on)?|arriving(?:\s+on)?|entrega"),
    ("departure", r"fecha\s+de\s+(?:salida|ida|viaje|inicio)|salida|salimos|sale(?:\s+el)?|departure(?:\s+date)?|departs?(?:\s+on)?|"
                  r"departing|outbound|ida|vuelo|check-?in|despega|embarque|start\s+date"),
    ("return", r"fecha\s+de\s+(?:regreso|vuelta)|regreso|vuelta|return(?:\s+date)?|returns?(?:\s+on)?|inbound|check-?out"),
    ("purchase", r"fecha\s+de\s+(?:la\s+)?(?:compra|venta|pedido|operacion|transaccion)|fecha\s+compra|order\s+date|"
                 r"date\s+of\s+(?:purchase|order)|purchase\s+date|comprad[oa]\s+el|pedido\s+realizado\s+el|pedido\s+del|compra\s+del|"
                 r"ordered\s+on|purchased\s+on"),
    ("renewal", r"proxima\s+renovacion|fecha\s+de\s+renovacion|renovacion|se\s+renovara(?:\s+(?:automaticamente\s+)?el)?|"
                r"renews?(?:\s+(?:automatically\s+)?on)?|renewal\s+date|next\s+renewal|prorroga"),
]
_ROLE_RX = [(role, re.compile(r"(?<![a-z])(?:" + src + r")(?![a-z])")) for role, src in _ROLE_SRC]
_FUTURE_ROLES = {"due", "expires", "delivery", "departure", "return", "renewal"}
_PAST_ROLES = {"issued", "purchase"}
_LABEL_WINDOW = 80
_LABEL_GAP = 24


@dataclass
class DateHit:
    """A date found in text: ``date``, the ``start``/``end`` offsets of its text, the ``role`` of the label in front
    of it (``""`` when there is none) and the matched ``text``."""
    date: date
    start: int
    end: int
    role: str = ""
    text: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"date": self.date.isoformat(), "start": self.start, "end": self.end, "role": self.role, "text": self.text}


# ---------------------------------------------------------------------------------------------- date arithmetic
def parse_iso(value: Any) -> Optional[date]:
    """A ``date`` from a date, a datetime or the first ``YYYY-MM-DD`` of a string; None when there is none."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    m = re.match(r"\s*([0-9]{4})-([0-9]{2})-([0-9]{2})", str(value or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def iso_day(value: Any) -> str:
    """``"YYYY-MM-DD"`` of a date, datetime or ISO string; ``""`` when it is none of them."""
    d = parse_iso(value)
    return d.isoformat() if d else ""


def _back(d: date, like: Any) -> Any:
    return d.isoformat() if isinstance(like, str) else d


def add_days(value: DateLike, days: int) -> Any:
    d = parse_iso(value)
    if d is None:
        raise ValueError(f"not a date: {value!r}")
    return _back(d + timedelta(days=int(days)), value)


def add_months(value: DateLike, months: int) -> Any:
    """The same day number ``months`` later; a month that is too short gives its last day (31 Jan + 1 month =
    28 or 29 Feb). A string in gives a string out."""
    d = parse_iso(value)
    if d is None:
        raise ValueError(f"not a date: {value!r}")
    index = d.year * 12 + (d.month - 1) + int(months)
    year, month0 = divmod(index, 12)
    month = month0 + 1
    return _back(date(year, month, min(d.day, calendar.monthrange(year, month)[1])), value)


def days_between(a: DateLike, b: DateLike) -> Optional[int]:
    """``b - a`` in days (negative when ``b`` is earlier); None when either is not a date."""
    da, db = parse_iso(a), parse_iso(b)
    return None if da is None or db is None else (db - da).days


def next_weekday(value: DateLike, weekday: int, *, strict: bool = True) -> Any:
    """The next date that falls on ``weekday`` (0 = Monday); ``strict`` skips ``value`` itself when it already is one."""
    d = parse_iso(value)
    if d is None:
        raise ValueError(f"not a date: {value!r}")
    ahead = (int(weekday) - d.weekday()) % 7
    if ahead == 0 and strict:
        ahead = 7
    return _back(d + timedelta(days=ahead), value)


def _today(today: Any) -> date:
    d = parse_iso(today) if today is not None else None
    return d if d is not None else date.today()


def _make(y: int, m: int, d: int) -> Optional[date]:
    if not 1900 <= y <= 2100:
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def resolve_year(day: int, month: int, today: Any, prefer: str = "nearest") -> Optional[date]:
    """The date with this ``day`` and ``month`` in the year that fits ``today`` (last, this or next year):

    * ``"future"`` — the soonest one that is not more than 10 days ago (deadlines in mail);
    * ``"past"`` — the latest one that is not more than 45 days ahead (issue and purchase dates);
    * ``"next"`` — the first one on or after ``today``;
    * ``"nearest"`` — the closest to ``today``."""
    t = _today(today)
    cands = [c for c in (_make(y, month, day) for y in (t.year - 1, t.year, t.year + 1)) if c]
    if not cands:
        return None

    def score(c: date) -> int:
        diff = (c - t).days
        if prefer == "future":
            return diff if diff >= -10 else 10000 - diff
        if prefer == "past":
            return -diff if diff <= 45 else 10000 + diff
        if prefer == "next":
            return diff if diff >= 0 else 10000 - diff
        return abs(diff)

    best = cands[0]
    for c in cands[1:]:
        if score(c) < score(best):
            best = c
    return best


# ---------------------------------------------------------------------------------------------- finding dates
def _scan(folded: str, *, dayfirst: bool, loose: bool) -> list[tuple[int, int, Optional[int], int, int]]:
    """``(start, end, year|None, month, day)`` of every date in folded text, overlaps resolved in favour of the
    earlier and longer match."""
    raw: list[tuple[int, int, Optional[int], int, int]] = []
    for m in _RX_ISO.finditer(folded):
        if _make(int(m.group(1)), int(m.group(3)), int(m.group(4))):
            raw.append((m.start(), m.end(), int(m.group(1)), int(m.group(3)), int(m.group(4))))
    for m in _RX_NUM.finditer(folded):
        a, b = int(m.group(1)), int(m.group(3))
        day, month = (a, b) if dayfirst else (b, a)
        if month > 12 and day <= 12:
            day, month = month, day
        year = int(m.group(4))
        year = year + 2000 if len(m.group(4)) == 2 else year
        if _make(year, month, day):
            raw.append((m.start(), m.end(), year, month, day))
    for m in _RX_DMY.finditer(folded):
        month, day = _MONTH_LOOKUP[m.group(2)], int(m.group(1))
        if m.group(3):
            if _make(int(m.group(3)), month, day):
                raw.append((m.start(), m.end(), int(m.group(3)), month, day))
        elif 1 <= day <= 31:
            raw.append((m.start(), m.end(), None, month, day))
    for m in _RX_MDY.finditer(folded):
        month, day = _MONTH_LOOKUP[m.group(1)], int(m.group(2))
        if m.group(3):
            if _make(int(m.group(3)), month, day):
                raw.append((m.start(), m.end(), int(m.group(3)), month, day))
        elif 1 <= day <= 31:
            raw.append((m.start(), m.end(), None, month, day))
    if loose:
        for m in _RX_DM.finditer(folded):
            a, b = int(m.group(1)), int(m.group(2))
            day, month = (a, b) if dayfirst else (b, a)
            if month > 12 and day <= 12:
                day, month = month, day
            if 1 <= month <= 12 and 1 <= day <= 31:
                raw.append((m.start(), m.end(), None, month, day))
    raw.sort(key=lambda r: (r[0], -(r[1] - r[0])))
    out: list[tuple[int, int, Optional[int], int, int]] = []
    for r in raw:
        if out and r[0] < out[-1][1]:
            continue
        out.append(r)
    return out


def _label_role(context: str) -> str:
    best: tuple[int, int, str] = (-1, 0, "")
    for role, rx in _ROLE_RX:
        for m in rx.finditer(context):
            key = (m.end(), m.end() - m.start())
            if key > (best[0], best[1]):
                best = (key[0], key[1], role)
    if best[2] and len(context) - best[0] <= _LABEL_GAP:
        return best[2]
    return ""


def _dayfirst(dayfirst: Optional[bool], lang: str) -> bool:
    if dayfirst is not None:
        return bool(dayfirst)
    return str(lang or "es").lower() not in ("en-us", "us")


def find_dates(text: Any, *, today: Any = None, lang: str = "es", dayfirst: Optional[bool] = True,
               loose: bool = False) -> list[DateHit]:
    """Every date in ``text`` in reading order, with the ``role`` of the label that precedes it on its line.

    Understands ``2026-10-15``, ``15/10/2026`` (also ``-`` and ``.``; two-digit years are 20xx), ``15 de octubre de
    2026``, ``15 oct. 2026``, ``October 15th, 2026``, ``15 sept`` and, with ``loose=True``, ``15/10``. A missing year
    is resolved against ``today`` (default: the real today) as the role asks: future for due/expires/delivery/
    departure/return/renewal, past for issued/purchase, nearest otherwise. ``dayfirst`` decides ``03/04/2026``
    (``None``: month first only for ``lang="en-US"``). Impossible dates are skipped."""
    s = "" if text is None else str(text)
    folded = fold(s)
    t = _today(today)
    first = _dayfirst(dayfirst, lang)
    hits: list[DateHit] = []
    prev_end = 0
    for start, end, year, month, day in _scan(folded, dayfirst=first, loose=loose):
        line_start = folded.rfind("\n", 0, start) + 1
        seg_start = max(line_start, start - _LABEL_WINDOW, prev_end)
        role = _label_role(folded[seg_start:start])
        if year is None:
            prefer = "future" if role in _FUTURE_ROLES else "past" if role in _PAST_ROLES else "nearest"
            resolved = resolve_year(day, month, t, prefer)
        else:
            resolved = _make(year, month, day)
        prev_end = end
        if resolved is None:
            continue
        hits.append(DateHit(resolved, start, end, role, s[start:end]))
    return hits


def parse_date(text: Any, *, today: Any = None, lang: str = "es", dayfirst: Optional[bool] = True,
               prefer: str = "nearest") -> Optional[date]:
    """The first date in ``text`` (see :func:`find_dates` for the forms), or None. A date without a year is placed
    in the year that suits ``prefer`` (``nearest``, ``future``, ``past``, ``next``) and ``today``; ``15/10`` is
    accepted here."""
    s = "" if text is None else str(text)
    folded = fold(s)
    t = _today(today)
    for _start, _end, year, month, day in _scan(folded, dayfirst=_dayfirst(dayfirst, lang), loose=True):
        found = _make(year, month, day) if year is not None else resolve_year(day, month, t, prefer)
        if found:
            return found
    return None


# ---------------------------------------------------------------------------------------------- spoken due dates
_NUMBER_WORDS = {"un": 1, "una": 1, "uno": 1, "a": 1, "an": 1, "one": 1, "dos": 2, "two": 2, "tres": 3, "three": 3,
                 "cuatro": 4, "four": 4, "cinco": 5, "five": 5, "seis": 6, "six": 6, "siete": 7, "seven": 7,
                 "ocho": 8, "eight": 8, "nueve": 9, "nine": 9, "diez": 10, "ten": 10, "quince": 15, "veinte": 20,
                 "treinta": 30}
_RX_MORNING = re.compile(r"\b(?:por|de|en|esta|la|a la)\s+(?:la\s+)?manana\b")
_RX_IN = re.compile(r"\b(?:en|dentro de|in)\s+([0-9]+|[a-z]+)\s+(dias?|days?|semanas?|weeks?|meses|mes|months?)\b")
_RX_WEEKDAY = re.compile(r"\b(" + _WEEKDAY_FULL + r")\b")
_RX_EOM = re.compile(r"\b(?:a\s+)?(?:fin|final|finales)\s+de(?:l)?\s+mes\b|\bend\s+of\s+(?:the\s+)?month\b")
_RX_DAYNUM = re.compile(r"\b(?:el\s+)?dia\s+([0-9]{1,2})\b|\bel\s+([0-9]{1,2})\b(?!\s*(?:de\b|/|-))")
_RX_NEXT_WEEK = re.compile(r"\b(?:la\s+)?semana\s+que\s+viene\b|\b(?:la\s+)?proxima\s+semana\b|\bnext\s+week\b")
_RX_NEXT_MONTH = re.compile(r"\b(?:el\s+)?mes\s+que\s+viene\b|\b(?:el\s+)?proximo\s+mes\b|\bnext\s+month\b")


def parse_due(text: Any, *, today: Any = None, lang: str = "es", vague: bool = False) -> Optional[date]:
    """The day a spoken or written due date names, relative to ``today``; None when it does not name one day.

    Absolute dates first (``2026-10-15``, ``15/10/2026``, ``15/10``, ``15 de octubre``; without a year: the first one on
    or after ``today``), then ``hoy``, ``mañana``, ``pasado mañana``, ``en 3 días``, ``dentro de dos semanas``,
    ``en un mes``, a weekday (``el viernes``, ``próximo lunes``: always after ``today``), ``el día 15`` / ``el 15`` (this
    month, else next), ``fin de mes``. ``"por la mañana"`` is a time of day, not tomorrow. With ``vague=True`` ``la
    semana que viene`` gives the Monday of next week and ``el mes que viene`` the first of next month; by default they
    are None so a caller keeps the words instead of guessing. ``lang`` is accepted for symmetry: Spanish and English
    words are always understood."""
    if text is None or not str(text).strip():
        return None
    t = _today(today)
    words = fold(str(text), keep_length=False).strip()
    found = parse_date(words, today=t, prefer="next", dayfirst=True)
    if found:
        return found
    cleaned = _RX_MORNING.sub(" ", words)
    m = _RX_IN.search(cleaned)
    if m:
        token = m.group(1)
        amount = int(token) if token.isdigit() else _NUMBER_WORDS.get(token)
        if amount is not None and 0 < amount <= 366:
            unit = m.group(2)
            if unit.startswith(("dia", "day")):
                return t + timedelta(days=amount)
            if unit.startswith(("semana", "week")):
                return t + timedelta(weeks=amount)
            return add_months(t, amount)
    if re.search(r"\bpasado manana\b|\bday after tomorrow\b", cleaned):
        return t + timedelta(days=2)
    if re.search(r"\bmanana\b|\btomorrow\b", cleaned):
        return t + timedelta(days=1)
    if re.search(r"\bhoy\b|\btoday\b|\besta noche\b|\btonight\b", cleaned):
        return t
    if vague:
        if _RX_NEXT_WEEK.search(cleaned):
            return next_weekday(t, 0)
        if _RX_NEXT_MONTH.search(cleaned):
            return add_months(t.replace(day=1), 1)
    m = _RX_WEEKDAY.search(cleaned)
    if m:
        return next_weekday(t, _WEEKDAY_LOOKUP[m.group(1)])
    m = _RX_DAYNUM.search(cleaned)
    if m:
        day = int(m.group(1) or m.group(2))
        if 1 <= day <= 31:
            for offset in (0, 1):
                first = add_months(t.replace(day=1), offset)
                cand = _make(first.year, first.month, day)
                if cand and cand >= t:
                    return cand
    if _RX_EOM.search(cleaned):
        return add_months(t.replace(day=1), 1) - timedelta(days=1)
    return None
