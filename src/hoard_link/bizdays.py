"""Business days and Spanish public holidays. Standard library only (Python only: no Node twin).

``holidays(year, region)`` lists the national holidays plus those of an autonomous community, from the data tables below;
:class:`Calendar` answers "is this a business day", "N business days later", "the next business day" and "how many
business days between", and knows which weekdays each carrier delivers on (:data:`CARRIER_WEEKDAYS`), which is what a
delivery estimate needs.

**The tables are a best effort, not an official source.** Autonomous communities and the central government publish the
calendar of the next year every autumn and change it (moved holidays, new local days). Anything that matters (a payroll,
a legal deadline) must go through ``Calendar(region, extra=[...])`` with the official dates. Local holidays of a town
(San Isidro in Madrid city, ...) are not included either. Moving a holiday that falls on a Sunday to the next Monday
is data-driven (:data:`MOVE_SUNDAY`, per region ``move_sunday``): by default 1 January, 6 January, 6 December, 8
December and 25 December move, Catalonia moves nothing; pass ``move_sunday=[...]`` (``"MM-DD"`` strings) to override.

Weekdays follow ``datetime.date.weekday()``: Monday is 0, Sunday is 6. Dates go in as ``date`` or ISO text (``"2026-10-02"``)
and come out the same way (text in -> text out), like :mod:`hoard_link.dates`.

Replaces the weekday/holiday guesses of Phileas (delivery ETA), Tantalus (billing dates), Kafka (deadlines) and Ledger
(value dates).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional, Sequence, Union

__all__ = [
    "NATIONAL_FIXED", "NATIONAL_EASTER", "REGIONS", "MOVE_SUNDAY", "CARRIER_WEEKDAYS", "easter", "holidays", "Calendar",
    "delivery_weekdays",
]

DateLike = Union[date, str]

#: Spanish national holidays that are always on the same day (``"MM-DD"``, name).
NATIONAL_FIXED: list[tuple[str, str]] = [
    ("01-01", "Año Nuevo"), ("01-06", "Epifanía del Señor"), ("05-01", "Fiesta del Trabajo"), ("08-15", "Asunción de la Virgen"),
    ("10-12", "Fiesta Nacional de España"), ("11-01", "Todos los Santos"), ("12-06", "Día de la Constitución"),
    ("12-08", "Inmaculada Concepción"), ("12-25", "Navidad"),
]
#: National holidays relative to Easter Sunday (days, name).
NATIONAL_EASTER: list[tuple[int, str]] = [(-2, "Viernes Santo")]

#: Holidays added by an autonomous community: ``easter`` (offset, name), ``fixed`` ("MM-DD", name), optional ``move_sunday``.
REGIONS: dict[str, dict[str, Any]] = {
    "ES-MD": {"name": "Comunidad de Madrid", "easter": [(-3, "Jueves Santo")], "fixed": [("05-02", "Fiesta de la Comunidad de Madrid")]},
    "ES-CT": {"name": "Cataluña", "easter": [(1, "Lunes de Pascua")],
              "fixed": [("06-24", "San Juan"), ("09-11", "Diada Nacional de Cataluña"), ("12-26", "San Esteban")], "move_sunday": []},
    "ES-AN": {"name": "Andalucía", "easter": [(-3, "Jueves Santo")], "fixed": [("02-28", "Día de Andalucía")]},
    "ES-VC": {"name": "Comunitat Valenciana", "easter": [(1, "Lunes de Pascua")],
              "fixed": [("03-19", "San José"), ("06-24", "San Juan"), ("10-09", "Día de la Comunitat Valenciana")]},
    "ES-GA": {"name": "Galicia", "easter": [(-3, "Jueves Santo")], "fixed": [("05-17", "Día de las Letras Gallegas"), ("07-25", "Santiago Apóstol")]},
    "ES-PV": {"name": "País Vasco", "easter": [(-3, "Jueves Santo"), (1, "Lunes de Pascua")], "fixed": [("07-25", "Santiago Apóstol")]},
}
#: Fixed holidays that move to the next Monday when they fall on a Sunday.
MOVE_SUNDAY: list[str] = ["01-01", "01-06", "12-06", "12-08", "12-25"]

_MON_FRI = (0, 1, 2, 3, 4)
_MON_SAT = (0, 1, 2, 3, 4, 5)
#: Weekdays on which a carrier normally delivers (keys of ``hoard_link.tracking.CARRIERS``; ``"default"`` for the rest).
CARRIER_WEEKDAYS: dict[str, tuple[int, ...]] = {
    "default": _MON_FRI,
    "amazon": _MON_SAT, "correos": _MON_FRI, "correos_express": _MON_FRI, "seur": _MON_FRI, "gls": _MON_FRI, "mrw": _MON_FRI,
    "nacex": _MON_FRI, "ctt": _MON_FRI, "inpost": _MON_SAT, "ups": _MON_FRI, "dhl": _MON_FRI, "fedex": _MON_FRI, "tnt": _MON_FRI,
    "dpd": _MON_FRI, "paack": _MON_SAT, "zeleris": _MON_FRI, "ecoscooting": _MON_FRI, "postnl": _MON_SAT, "royalmail": _MON_SAT,
    "deutschepost": _MON_SAT, "laposte": _MON_SAT, "yunexpress": _MON_FRI, "cainiao": _MON_FRI, "chinapost": _MON_FRI,
}


def delivery_weekdays(carrier: Optional[str] = None) -> tuple[int, ...]:
    """Weekdays (Monday 0) a carrier delivers on; Monday to Friday when unknown."""
    return CARRIER_WEEKDAYS.get((carrier or "").lower(), CARRIER_WEEKDAYS["default"])


def easter(year: int) -> date:
    """Easter Sunday of a Gregorian year (Meeus / Jones / Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _region_key(region: Optional[str]) -> str:
    r = (region or "ES").strip().upper().replace("_", "-")
    if r in ("ES", "ES-"):
        return "ES"
    if "-" not in r and f"ES-{r}" in REGIONS:
        r = f"ES-{r}"
    if r not in REGIONS:
        raise ValueError(f"unknown region {region!r}; known: ES, {', '.join(sorted(REGIONS))}")
    return r


def holidays(year: int, region: str = "ES", *, move_sunday: Optional[Sequence[str]] = None) -> dict[date, str]:
    """``{date: name}`` of the public holidays of a year, sorted by date. ``region``: ``"ES"`` (national) or one of
    ``ES-MD``, ``ES-CT``, ``ES-AN``, ``ES-VC``, ``ES-GA``, ``ES-PV`` (the national ones plus the community's; ``"MD"``
    works too). A fixed holiday that lands on a Sunday and is in ``move_sunday`` (default: the region's, else
    :data:`MOVE_SUNDAY`) appears on the Monday with `` (trasladado)`` after its name. Unknown region -> ``ValueError``."""
    key = _region_key(region)
    info = REGIONS.get(key, {})
    moves = list(move_sunday) if move_sunday is not None else list(info.get("move_sunday", MOVE_SUNDAY))
    found: dict[date, str] = {}

    def put(day: date, name: str) -> None:
        found.setdefault(day, name)

    fixed = list(NATIONAL_FIXED) + list(info.get("fixed", []))
    for mmdd, name in fixed:
        month, day_n = int(mmdd[:2]), int(mmdd[3:])
        day = date(year, month, day_n)
        if day.weekday() == 6 and mmdd in moves:
            put(day + timedelta(days=1), f"{name} (trasladado)")
        else:
            put(day, name)
    sunday = easter(year)
    for offset, name in list(NATIONAL_EASTER) + list(info.get("easter", [])):
        put(sunday + timedelta(days=offset), name)
    return dict(sorted(found.items()))


def _as_date(value: DateLike) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def _out(like: DateLike, result: date) -> DateLike:
    return result.isoformat() if isinstance(like, str) else result


class Calendar:
    """Business-day arithmetic for a region.

    ``Calendar("ES-MD", extra=["2026-12-24"])``: the region's holidays plus your own days off (``extra``: dates or ISO
    text). ``weekend`` is the set of non-working weekdays (default Saturday and Sunday). ``move_sunday`` as in
    :func:`holidays`."""

    def __init__(self, region: str = "ES", extra: Iterable[DateLike] = (), *, weekend: Iterable[int] = (5, 6),
                 move_sunday: Optional[Sequence[str]] = None) -> None:
        self.region = _region_key(region)
        self.weekend = frozenset(weekend)
        if len(self.weekend & set(range(7))) >= 7:
            raise ValueError("a calendar needs at least one working weekday")
        self.move_sunday = None if move_sunday is None else list(move_sunday)
        self.extra = frozenset(_as_date(d) for d in extra)
        self._years: dict[int, dict[date, str]] = {}

    def holidays(self, year: int) -> dict[date, str]:
        """Holidays of the year (region's plus ``extra``)."""
        if year not in self._years:
            table = holidays(year, self.region, move_sunday=self.move_sunday)
            for d in self.extra:
                if d.year == year:
                    table.setdefault(d, "Día no laborable")
            self._years[year] = dict(sorted(table.items()))
        return self._years[year]

    def holiday_name(self, day: DateLike) -> Optional[str]:
        d = _as_date(day)
        return self.holidays(d.year).get(d)

    def is_holiday(self, day: DateLike) -> bool:
        return self.holiday_name(day) is not None

    def is_business_day(self, day: DateLike) -> bool:
        d = _as_date(day)
        return d.weekday() not in self.weekend and d not in self.holidays(d.year)

    def add_business_days(self, day: DateLike, n: int) -> DateLike:
        """The date ``n`` business days after ``day`` (before it when negative); ``day`` itself is not counted, and
        ``n == 0`` returns it unchanged even when it is not a business day."""
        d = _as_date(day)
        step = 1 if n >= 0 else -1
        left = abs(int(n))
        while left:
            d += timedelta(days=step)
            if self.is_business_day(d):
                left -= 1
        return _out(day, d)

    def next_business_day(self, day: DateLike, *, include_self: bool = False) -> DateLike:
        """The first business day after ``day`` (on or after it with ``include_self=True``)."""
        d = _as_date(day)
        if not include_self or not self.is_business_day(d):
            d += timedelta(days=1)
            while not self.is_business_day(d):
                d += timedelta(days=1)
        return _out(day, d)

    def previous_business_day(self, day: DateLike, *, include_self: bool = False) -> DateLike:
        """The last business day before ``day`` (on or before it with ``include_self=True``)."""
        d = _as_date(day)
        if not include_self or not self.is_business_day(d):
            d -= timedelta(days=1)
            while not self.is_business_day(d):
                d -= timedelta(days=1)
        return _out(day, d)

    def business_days_between(self, start: DateLike, end: DateLike) -> int:
        """Business days in ``[start, end)`` (``start`` counts, ``end`` does not); negative when ``end`` is earlier."""
        a, b = _as_date(start), _as_date(end)
        sign = 1
        if b < a:
            a, b, sign = b, a, -1
        count, d = 0, a
        while d < b:
            if self.is_business_day(d):
                count += 1
            d += timedelta(days=1)
        return sign * count

    def is_delivery_day(self, day: DateLike, carrier: Optional[str] = None) -> bool:
        """A day the carrier delivers on: one of its weekdays (:data:`CARRIER_WEEKDAYS`) that is not a holiday."""
        d = _as_date(day)
        return d.weekday() in delivery_weekdays(carrier) and d not in self.holidays(d.year)

    def next_delivery_day(self, day: DateLike, carrier: Optional[str] = None, *, include_self: bool = False) -> DateLike:
        """The first delivery day of the carrier after ``day`` (on or after with ``include_self=True``)."""
        d = _as_date(day)
        if not include_self or not self.is_delivery_day(d, carrier):
            d += timedelta(days=1)
            while not self.is_delivery_day(d, carrier):
                d += timedelta(days=1)
        return _out(day, d)
