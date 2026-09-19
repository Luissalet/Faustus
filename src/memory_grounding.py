"""src/memory_grounding.py — grounding lint for compiled knowledge (FAUSTUS).

A memory item, a curated fact, or a document summary is only as trustworthy
as the evidence behind it. Nothing before this module checked that at read
time: an item could carry a number, a date or a name that never actually
appeared in the text it cites as its source, and it would sit in the store
looking exactly as credible as one that did. This module is the check —
100% deterministic and LLM-free, the same discipline
``src/memory_curator.py`` and ``src/memory_conflicts.py`` already hold to,
so the same store and the same clock always produce the same report.

Three layers, cheapest first:

1. ``extract_specifics(text)`` — pull out the concrete, checkable claims in
   a piece of text: numbers (with units/percent/currency), dates (several
   English and Spanish spellings), quoted strings, proper-noun-ish
   multiword capitalized names, URLs and email-like handles. Table-driven
   regexes, nothing learned, nothing guessed.
2. ``check_item(claim_text, evidence_texts)`` — every specific extracted
   from a claim must show up, after normalisation, in at least one piece of
   evidence. Numbers are compared as numbers (``1.000`` and ``1,000`` and
   ``1000`` are the same number); dates are compared as dates (day/month/
   year, tolerant of a few equivalent spellings); everything else is
   compared as normalised text.
3. ``lint(owner, limit=...)`` — runs (2) over the memory items that have
   retrievable evidence (``item["evidence"][*]["excerpt"]``,
   ``src/memory_engine.normalize_evidence``). An item with no evidence at
   all is not accused of anything — it is counted as ``unverifiable`` and
   left out of the findings, because "no evidence" and "evidence
   contradicted by the claim" are different problems and only the second
   one is this module's to flag.

What this module never does: call a model, edit or delete a memory item, or
treat the ABSENCE of evidence as proof of fabrication. It only ever says
"this specific claim is not backed by the evidence cited for it".
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# extract_specifics — table-driven regexes, English + Spanish
# ---------------------------------------------------------------------------

_MONTHS_EN: Dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_MONTHS_ES: Dict[str, int] = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
_MONTHS_ALL: Dict[str, int] = {**_MONTHS_EN, **_MONTHS_ES}
_MONTH_WORD = "|".join(sorted((re.escape(m) for m in _MONTHS_ALL), key=len, reverse=True))

#: currency symbols / ISO codes recognised as a "number is money" marker.
_CURRENCY_SYMBOLS = "€$£¥"
_CURRENCY_CODES = ("usd", "eur", "gbp", "mxn", "jpy", "cop", "ars", "clp", "pen", "brl")

#: a small closed set of unit words worth tagging onto a number (not
#: exhaustive by design — this is a lint, not a units parser).
_UNIT_WORDS = (
    "km", "kg", "g", "mg", "m", "cm", "mm", "h", "hr", "hrs", "hora", "horas",
    "min", "mins", "minuto", "minutos", "s", "sec", "seg", "segundos",
    "ms", "gb", "mb", "kb", "tb", "%",
)

_NUMBER_CORE = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"

_NUMBER_RE = re.compile(
    rf"""
    (?P<currency_pre>[{_CURRENCY_SYMBOLS}])?\s?
    (?P<number>{_NUMBER_CORE})
    \s?
    (?P<percent>%)?
    (?:\s?(?P<unit>{'|'.join(re.escape(u) for u in _UNIT_WORDS if u != '%')}|
        {'|'.join(_CURRENCY_CODES)}))?
    """,
    re.IGNORECASE | re.VERBOSE,
)

_ISO_DATE_RE = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\b")

_ES_LONG_DATE_RE = re.compile(
    rf"\b(?P<d>\d{{1,2}})\s+de\s+(?P<mon>{_MONTH_WORD})\s+de\s+(?P<y>\d{{4}})\b",
    re.IGNORECASE,
)

_EN_LONG_DATE_RE = re.compile(
    rf"\b(?P<mon>{_MONTH_WORD})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,?\s+(?P<y>\d{{4}}))?\b",
    re.IGNORECASE,
)

_SLASH_DATE_RE = re.compile(r"\b(?P<a>\d{1,2})/(?P<b>\d{1,2})/(?P<y>\d{2,4})\b")

_QUOTE_RE = re.compile(
    r'"([^"\n]{2,200})"'
    r'|“([^”\n]{2,200})”'
    r"|«([^»\n]{2,200})»",
)

_PROPER_NOUN_RE = re.compile(
    r"\b([A-ZÁÉÍÓÚÑ][\w'-]*(?:\s+[A-ZÁÉÍÓÚÑ][\w'-]*){1,5})\b"
)

_URL_RE = re.compile(r"\bhttps?://[^\s)>\]\"']+", re.IGNORECASE)

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

#: leading capitalized word that is just sentence-start (or Spanish "de"
#: connective) — filtered out of proper-noun matches so "The project" or
#: "El proyecto" alone don't count as a name.
_STOP_PROPER = frozenset({
    "the", "a", "an", "this", "that", "these", "those",
    "el", "la", "los", "las", "un", "una", "unos", "unas", "este", "esta",
})


def _mask(text: str, spans: Sequence[Tuple[int, int]]) -> str:
    """Blank out already-claimed spans with spaces (keeps offsets stable)
    so a later, looser pattern can't re-claim the same characters (a date's
    day number showing up again as a bare "number" specific, etc.)."""
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        for i in range(start, min(end, len(chars))):
            chars[i] = " "
    return "".join(chars)


def normalize_number(raw: str) -> Optional[float]:
    """"1.000" / "1,000" / "1000" -> 1000.0; "19,5" / "19.5" -> 19.5.

    Heuristic (documented, not a real locale parser): the LAST separator in
    the string is the decimal point when 1-2 digits follow it; otherwise
    (3 digits follow it, or nothing does) every separator is a thousands
    grouping mark. Any separator before the last one is always a thousands
    mark.
    """
    s = str(raw or "").strip()
    if not s:
        return None
    seps = [i for i, ch in enumerate(s) if ch in ".,"]
    if not seps:
        try:
            return float(s)
        except ValueError:
            return None
    last = seps[-1]
    after = s[last + 1:]
    decimal_last = len(after) in (1, 2) and after.isdigit()
    if decimal_last:
        mantissa = s[:last].translate(str.maketrans("", "", ".,"))
        try:
            return float(f"{mantissa}.{after}")
        except ValueError:
            return None
    cleaned = s.translate(str.maketrans("", "", ".,"))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _norm_text(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _norm_loose(text: Any) -> str:
    """Casefolded, accent-stripped, whitespace-collapsed — for comparing
    proper nouns and quotes across small spelling drift."""
    return _strip_accents(_norm_text(text))


def extract_specifics(text: Any) -> List[Dict[str, Any]]:
    """The concrete, checkable claims in ``text``.

    Each item is ``{"type": ..., "value": <as written>, "norm": <for
    comparison>}``. Order of extraction matters: URLs, emails, quotes and
    dates are claimed first and masked out, so a date's "19" is never also
    reported as a bare number, and a quoted sentence's capitalized words
    are never also reported as a proper noun.
    """
    raw = str(text or "")
    if not raw.strip():
        return []

    out: List[Dict[str, Any]] = []
    claimed: List[Tuple[int, int]] = []
    working = raw

    # 1) URLs
    for m in _URL_RE.finditer(working):
        out.append({"type": "url", "value": m.group(0),
                    "norm": m.group(0).rstrip("/").casefold()})
        claimed.append(m.span())
    working = _mask(working, claimed)

    # 2) email-like handles
    email_spans: List[Tuple[int, int]] = []
    for m in _EMAIL_RE.finditer(working):
        out.append({"type": "email", "value": m.group(0),
                    "norm": m.group(0).casefold()})
        email_spans.append(m.span())
    working = _mask(working, email_spans)
    claimed.extend(email_spans)

    # 3) quoted strings
    quote_spans: List[Tuple[int, int]] = []
    for m in _QUOTE_RE.finditer(working):
        inner = next(g for g in m.groups() if g)
        if inner.strip():
            out.append({"type": "quote", "value": inner.strip(),
                        "norm": _norm_loose(inner)})
        quote_spans.append(m.span())
    working = _mask(working, quote_spans)
    claimed.extend(quote_spans)

    # 4) dates — ISO, Spanish long form, English long form, D/M/Y slash form
    date_spans: List[Tuple[int, int]] = []
    for m in _ISO_DATE_RE.finditer(working):
        y, mo, d = int(m["y"]), int(m["m"]), int(m["d"])
        if 1 <= mo <= 12 and 1 <= d <= 31:
            out.append({"type": "date", "value": m.group(0), "norm": (y, mo, d)})
            date_spans.append(m.span())
    working = _mask(working, date_spans)
    claimed.extend(date_spans)

    for pattern in (_ES_LONG_DATE_RE, _EN_LONG_DATE_RE):
        spans: List[Tuple[int, int]] = []
        for m in pattern.finditer(working):
            mon = _MONTHS_ALL.get(m["mon"].lower())
            if not mon:
                continue
            d = int(m["d"])
            if not (1 <= d <= 31):
                continue
            y = int(m["y"]) if m.groupdict().get("y") else None
            out.append({"type": "date", "value": m.group(0),
                        "norm": (y, mon, d)})
            spans.append(m.span())
        working = _mask(working, spans)
        claimed.extend(spans)

    slash_spans: List[Tuple[int, int]] = []
    for m in _SLASH_DATE_RE.finditer(working):
        a, b, y = int(m["a"]), int(m["b"]), int(m["y"])
        y = y + 2000 if y < 100 else y
        # Ambiguous day/month order: keep both readings, either is a match.
        candidates = []
        if 1 <= a <= 12 and 1 <= b <= 31:
            candidates.append((y, a, b))
        if 1 <= b <= 12 and 1 <= a <= 31:
            candidates.append((y, b, a))
        if candidates:
            out.append({"type": "date", "value": m.group(0),
                        "norm": tuple(candidates)})
            slash_spans.append(m.span())
    working = _mask(working, slash_spans)
    claimed.extend(slash_spans)

    # 5) numbers (with optional unit/%/currency)
    number_spans: List[Tuple[int, int]] = []
    for m in _NUMBER_RE.finditer(working):
        value = normalize_number(m["number"])
        if value is None:
            continue
        number_spans.append(m.span())
        kind = "number"
        if m["percent"]:
            kind = "percent"
        elif m["currency_pre"] or (m["unit"] and m["unit"].lower() in _CURRENCY_CODES):
            kind = "currency"
        out.append({"type": kind, "value": m.group(0).strip(), "norm": round(value, 6)})
    working = _mask(working, number_spans)
    claimed.extend(number_spans)

    # 6) proper-noun-ish multiword capitalized names, on what's left
    for m in _PROPER_NOUN_RE.finditer(working):
        words = m.group(1).split()
        if words and words[0].casefold() in _STOP_PROPER:
            words = words[1:]
        if len(words) < 2:
            continue
        value = " ".join(words)
        out.append({"type": "proper_noun", "value": value, "norm": _norm_loose(value)})

    return out


# ---------------------------------------------------------------------------
# check_item — does the evidence back up the claim's specifics?
# ---------------------------------------------------------------------------

_NUMBER_EPS = 1e-6


def _date_matches(claim_norm: Any, evidence_dates: Sequence[Any]) -> bool:
    candidates = claim_norm if isinstance(claim_norm, tuple) and claim_norm and isinstance(claim_norm[0], tuple) else (claim_norm,)
    for cand in candidates:
        cy, cm, cd = cand
        for ev in evidence_dates:
            ev_candidates = ev if isinstance(ev, tuple) and ev and isinstance(ev[0], tuple) else (ev,)
            for ey, em, ed in ev_candidates:
                if cm != em or cd != ed:
                    continue
                if cy is None or ey is None or cy == ey:
                    return True
    return False


def check_item(claim_text: Any, evidence_texts: Sequence[Any]) -> Dict[str, Any]:
    """Every specific in ``claim_text`` must be backed by at least one text
    in ``evidence_texts``. Returns ``{"grounded", "missing", "checked"}``.

    An empty (or all-unextractable) claim is trivially grounded — this
    function never invents a problem for prose with nothing checkable in it.
    """
    specifics = extract_specifics(claim_text)
    checked = len(specifics)
    if checked == 0:
        return {"grounded": True, "missing": [], "checked": 0}

    evidence_blob = " \n ".join(_norm_loose(e) for e in evidence_texts if str(e or "").strip())
    evidence_specifics: List[Dict[str, Any]] = []
    for e in evidence_texts:
        evidence_specifics.extend(extract_specifics(e))
    evidence_numbers = [s["norm"] for s in evidence_specifics
                        if s["type"] in ("number", "percent", "currency")]
    evidence_dates = [s["norm"] for s in evidence_specifics if s["type"] == "date"]

    missing: List[Dict[str, Any]] = []
    for spec in specifics:
        found = False
        if spec["type"] in ("number", "percent", "currency"):
            found = any(abs(spec["norm"] - n) < _NUMBER_EPS for n in evidence_numbers)
        elif spec["type"] == "date":
            found = _date_matches(spec["norm"], evidence_dates)
        else:
            found = bool(spec["norm"]) and spec["norm"] in evidence_blob
        if not found:
            missing.append({"type": spec["type"], "value": spec["value"]})

    return {"grounded": len(missing) == 0, "missing": missing, "checked": checked}


# ---------------------------------------------------------------------------
# lint — run over the memory store
# ---------------------------------------------------------------------------

DEFAULT_LINT_LIMIT = 200


def _item_evidence_texts(item: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for span in item.get("evidence") or []:
        if not isinstance(span, dict):
            continue
        excerpt = str(span.get("excerpt") or "").strip()
        if excerpt:
            out.append(excerpt)
    return out


def lint(owner: Optional[str] = None, limit: int = DEFAULT_LINT_LIMIT) -> Dict[str, Any]:
    """Grounding findings for one owner's active memory items.

    Items with no retrievable evidence at all are counted as
    ``unverifiable`` and never flagged — absence of evidence is not, on its
    own, evidence of fabrication. Findings are sorted by how many specifics
    are missing, worst first.
    """
    try:
        from src import memory_engine as engine

        items = engine.list_items(owner=owner, status="active",
                                  limit=max(1, min(2000, int(limit or DEFAULT_LINT_LIMIT))))
    except Exception as exc:  # noqa: BLE001 - a lint must never break its caller
        logger.debug("memory_grounding: lint failed to load items (%s)", exc)
        return {"findings": [], "unverifiable": 0, "checked_items": 0, "total_items": 0}

    findings: List[Dict[str, Any]] = []
    unverifiable = 0
    checked_items = 0
    for item in items:
        evidence_texts = _item_evidence_texts(item)
        if not evidence_texts:
            unverifiable += 1
            continue
        checked_items += 1
        text = str(item.get("text") or "")
        try:
            result = check_item(text, evidence_texts)
        except Exception as exc:  # noqa: BLE001
            logger.debug("memory_grounding: check_item failed for %s (%s)",
                        item.get("id"), exc)
            continue
        if result["grounded"]:
            continue
        findings.append({
            "id": str(item.get("id") or ""),
            "text": text,
            "category": item.get("category") or item.get("level") or "",
            "missing": result["missing"],
            "checked": result["checked"],
            "evidence_count": len(evidence_texts),
            "updated_at": item.get("updated_at") or "",
        })

    findings.sort(key=lambda f: len(f["missing"]), reverse=True)
    return {
        "findings": findings,
        "unverifiable": unverifiable,
        "checked_items": checked_items,
        "total_items": len(items),
    }


__all__ = [
    "extract_specifics", "normalize_number", "check_item", "lint",
    "DEFAULT_LINT_LIMIT",
]
