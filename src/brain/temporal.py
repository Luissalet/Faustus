"""brain/temporal.py — reading "when" out of a plain sentence.

A memory item already has ``valid_from``/``valid_until`` columns
(``src/memory_engine.py``); nothing before this module ever filled them from
the TEXT itself. "Ada worked at Cordera Labs until March 2025" carries its
own validity window in the sentence — a human should never have to fill a
date picker to say something this ordinary.

``parse_temporal`` is a small, table-driven, deterministic parser (Spanish
+ English). It is deliberately conservative: a bare capitalized word that
happens to be a month name ("Marzo" as the first word of a sentence, e.g. a
person's name) must NEVER be read as a date. A month only counts once it
sits in date-ish company — a marker before it ("desde marzo", "since
March"), a year after it ("marzo de 2025", "March 2025"), or one of the
relative phrases below. No marker, no date-ish company at all -> every
field comes back ``None``: "never guess" is the whole point of this module.

Three independent things can be found in one sentence, and any combination
of them may be present at once:

* a **validity window** — ``valid_from``/``valid_until``, from an explicit
  date/month/year, a "desde"/"since"/"from" or "hasta"/"until"/"till"
  marker, an "entre X y Y"/"between X and Y" range, or a relative phrase
  ("el año pasado", "this month") relative to ``now``. A dated mention with
  no directional marker at all ("marzo de 2025", "en 2024") is read as the
  START of validity — the ordinary reading of "X happened in March 2025".
* a **state** — ``"past"`` for "ya no"/"no longer"/"used to"/"antes"/
  "solía" (a fact that stopped being true, but with NO invented date: the
  caller decides what to do with a state and no window), ``"current"`` for
  "ahora"/"now"/"currently"/"actualmente", ``"future"`` for a small set of
  forward-looking phrases.
* the raw ``markers`` that were matched, for anyone who wants to show what
  triggered the parse (a debug view, a tooltip) without re-parsing.

Nothing here touches a store or calls a model — this is a pure function of
(``text``, ``now``), unit-tested in isolation.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

_MONTHS: Dict[str, int] = {
    # Spanish
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}
# Longest-first so "septiembre" is tried before a shorter alternative could
# swallow part of it (none currently overlap, but this keeps it safe as the
# table grows).
_MONTH_ALT = "|".join(sorted((re.escape(m) for m in _MONTHS), key=len, reverse=True))

_SINCE_WORDS = r"(?:a\s+partir\s+de|desde|since|from)"
_UNTIL_WORDS = r"(?:hasta|until|till)"
_BETWEEN_WORDS = r"(?:entre|between)"

# State markers never invent a date on their own.
_PAST_RE = re.compile(r"\b(?:ya\s+no|no\s+longer|used\s+to|antes|solia)\b", re.IGNORECASE)
_CURRENT_RE = re.compile(r"\b(?:ahora|now|currently|actualmente)\b", re.IGNORECASE)
_FUTURE_RE = re.compile(
    r"\b(?:en\s+el\s+futuro|proximamente|in\s+the\s+future|soon)\b", re.IGNORECASE
)


def _date_alt_pattern(tag: str, *, bare_year: bool = False, bare_month: bool = False,
                       year_needs_context: bool = False) -> str:
    """One alternation of every date SHAPE this module understands, with
    every named group suffixed by ``tag`` so several copies (e.g. the two
    sides of "entre X y Y") can live in the same compiled regex without
    colliding on group names.

    ``bare_year``/``bare_month`` control the two forms that are only safe in
    certain grammatical positions: a bare month name ("marzo" with no year)
    is only ever allowed right after a since/until marker (the marker IS
    the date-ish context); a bare year is allowed standalone only when
    ``year_needs_context`` also requires an immediately preceding "en"/
    "in"/"on" — this is what keeps "2024" from being read out of an
    unrelated sentence containing a stray four-digit number.
    """
    parts = [
        rf"(?P<iso_{tag}>\d{{4}}-\d{{2}}-\d{{2}})",
        rf"(?P<sfd_{tag}>\d{{1,2}})/(?P<sfm_{tag}>\d{{1,2}})/(?P<sfy_{tag}>\d{{4}})",
        rf"(?P<smm_{tag}>\d{{1,2}})/(?P<smy_{tag}>\d{{4}})",
        rf"(?P<mym_{tag}>{_MONTH_ALT})(?:\s+de)?\s+(?P<myy_{tag}>\d{{4}})",
        rf"(?P<rly_{tag}>el\s+ano\s+pasado|last\s+year)",
        rf"(?P<rty_{tag}>este\s+ano|this\s+year)",
        rf"(?P<rtm_{tag}>este\s+mes|this\s+month)",
        rf"(?P<rlm_{tag}>el\s+mes\s+pasado|last\s+month)",
    ]
    if bare_year:
        if year_needs_context:
            parts.append(rf"(?:en|in|on)\s+(?P<by_{tag}>\d{{4}})")
        else:
            parts.append(rf"(?P<by_{tag}>\d{{4}})")
    if bare_month:
        parts.append(rf"(?P<mo_{tag}>{_MONTH_ALT})")
    return "(?:" + "|".join(parts) + ")"


# Argument of a since/until marker: the marker word is already the context,
# so a bare month or a bare year need no extra prefix here.
_SINCE_RE = re.compile(
    rf"\b(?P<marker>{_SINCE_WORDS})\s+" + _date_alt_pattern("x", bare_year=True, bare_month=True),
    re.IGNORECASE,
)
_UNTIL_RE = re.compile(
    rf"\b(?P<marker>{_UNTIL_WORDS})\s+" + _date_alt_pattern("x", bare_year=True, bare_month=True),
    re.IGNORECASE,
)
_BETWEEN_RE = re.compile(
    rf"\b(?P<marker>{_BETWEEN_WORDS})\s+"
    + _date_alt_pattern("a", bare_year=True, bare_month=True)
    + r"\s+(?:y|and)\s+"
    + _date_alt_pattern("b", bare_year=True, bare_month=True),
    re.IGNORECASE,
)
# Standalone mention, no directional marker at all ("marzo de 2025", "en
# 2024"): month+year and the explicit date shapes stand on their own; a
# bare month WITHOUT a year never matches here (that is exactly the "Marzo"
# person's-name case the module must stay conservative about), and a bare
# year is only recognised right after "en"/"in"/"on".
_STANDALONE_RE = re.compile(
    r"\b" + _date_alt_pattern("s", bare_year=True, bare_month=False, year_needs_context=True) + r"\b",
    re.IGNORECASE,
)


def _fold_for_match(text: str) -> str:
    """Lowercase, accent-stripped, SAME LENGTH as ``text`` (so a regex match
    span on this copy slices the same characters out of the original)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


# ---------------------------------------------------------------------------
# Period arithmetic
# ---------------------------------------------------------------------------


def _day_period(year: int, month: int, day: int) -> Tuple[datetime, datetime]:
    start = datetime(year, month, day, tzinfo=timezone.utc)
    return start, start.replace(hour=23, minute=59, second=59)


def _month_period(year: int, month: int) -> Tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    last_day = calendar.monthrange(year, month)[1]
    return start, datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)


def _year_period(year: int) -> Tuple[datetime, datetime]:
    return (datetime(year, 1, 1, tzinfo=timezone.utc),
            datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc))


def _period_from_values(*, iso=None, sfd=None, sfm=None, sfy=None, smm=None, smy=None,
                        mym=None, myy=None, rly=None, rty=None, rtm=None, rlm=None,
                        by=None, mo=None, now: datetime) -> Optional[Tuple[datetime, datetime]]:
    """The (start, end) instants covered by whichever single date-shape
    matched — exactly one of these arguments is ever non-empty per call."""
    try:
        if iso:
            y, m, d = (int(x) for x in iso.split("-"))
            return _day_period(y, m, d)
        if sfy and sfm and sfd:
            return _day_period(int(sfy), int(sfm), int(sfd))
        if smy and smm:
            return _month_period(int(smy), int(smm))
        if myy and mym:
            month = _MONTHS.get(mym.lower())
            return _month_period(int(myy), month) if month else None
        if rly:
            return _year_period(now.year - 1)
        if rty:
            return _year_period(now.year)
        if rtm:
            return _month_period(now.year, now.month)
        if rlm:
            year, month = now.year, now.month - 1
            if month == 0:
                month, year = 12, year - 1
            return _month_period(year, month)
        if by:
            return _year_period(int(by))
        if mo:
            month = _MONTHS.get(mo.lower())
            return _month_period(now.year, month) if month else None
    except ValueError:
        return None
    return None


def _extract_period(groups: Dict[str, Optional[str]], tag: str,
                    now: datetime) -> Optional[Tuple[datetime, datetime]]:
    def g(name: str) -> Optional[str]:
        return groups.get(f"{name}_{tag}")

    return _period_from_values(
        iso=g("iso"), sfd=g("sfd"), sfm=g("sfm"), sfy=g("sfy"),
        smm=g("smm"), smy=g("smy"), mym=g("mym"), myy=g("myy"),
        rly=g("rly"), rty=g("rty"), rtm=g("rtm"), rlm=g("rlm"),
        by=g("by"), mo=g("mo"), now=now,
    )


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_temporal(text: Any, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Read a validity window and/or a state out of one piece of text.

    Returns ``{"valid_from": iso|None, "valid_until": iso|None,
    "state": "past"|"current"|"future"|None, "markers": [str]}``. Every
    field is independently optional; a text with nothing date-ish AND no
    state word in it comes back with every field ``None``/empty — this
    function never invents a date.
    """
    now = now if (now is not None and now.tzinfo) else (
        now.replace(tzinfo=timezone.utc) if now is not None else datetime.now(timezone.utc)
    )
    raw = str(text or "")
    if not raw.strip():
        return {"valid_from": None, "valid_until": None, "state": None, "markers": []}
    norm = _fold_for_match(raw)

    markers: List[str] = []
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None

    match = _BETWEEN_RE.search(norm)
    if match:
        gd = match.groupdict()
        period_a = _extract_period(gd, "a", now)
        period_b = _extract_period(gd, "b", now)
        if period_a and period_b:
            valid_from, valid_until = period_a[0], period_b[1]
            markers.append(raw[match.start():match.end()])

    if valid_from is None:
        match = _SINCE_RE.search(norm)
        if match:
            period = _extract_period(match.groupdict(), "x", now)
            if period:
                valid_from = period[0]
                markers.append(raw[match.start():match.end()])

    if valid_until is None:
        match = _UNTIL_RE.search(norm)
        if match:
            period = _extract_period(match.groupdict(), "x", now)
            if period:
                valid_until = period[1]
                markers.append(raw[match.start():match.end()])

    if valid_from is None and valid_until is None:
        match = _STANDALONE_RE.search(norm)
        if match:
            period = _extract_period(match.groupdict(), "s", now)
            if period:
                valid_from = period[0]
                markers.append(raw[match.start():match.end()])

    state: Optional[str] = None
    match = _PAST_RE.search(norm)
    if match:
        state = "past"
        markers.append(raw[match.start():match.end()])
    else:
        match = _CURRENT_RE.search(norm)
        if match:
            state = "current"
            markers.append(raw[match.start():match.end()])
        else:
            match = _FUTURE_RE.search(norm)
            if match:
                state = "future"
                markers.append(raw[match.start():match.end()])

    return {
        "valid_from": _to_iso(valid_from) if valid_from else None,
        "valid_until": _to_iso(valid_until) if valid_until else None,
        "state": state,
        "markers": markers,
    }


__all__ = ["parse_temporal"]
