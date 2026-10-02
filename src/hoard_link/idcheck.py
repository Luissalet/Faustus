"""Identifiers with a check digit: Spanish ID numbers, IBAN, cards, product codes, and personal data in free text.

Standard library only. The Node twin is ``js/hoard-commons/idcheck.js`` (same names in camelCase); both are checked
against ``tests/vectors/idcheck.json``.

* Validators — :func:`luhn_ok`, :func:`iban_ok` (mod 97 and the country's length), :func:`dni_ok`, :func:`nie_ok`,
  :func:`cif_ok` (control digit or letter by entity type), :func:`nif_ok`, :func:`ean_ok` (EAN-8, UPC-A/EAN-12,
  EAN-13, GTIN-14), :func:`isbn_ok`.
* Product ids — :func:`asin`, :func:`identifiers` (EAN, ASIN, ISBN and shop SKUs found in text and links).
* Personal data — :func:`scan_pii` finds DNI, NIE, CIF, IBAN, CARD, EMAIL and PHONE_ES, **counting only values whose
  checksum is right** (a reference number that merely looks like an ID is left alone); :func:`mask_text` and
  :func:`mask_obj` replace them with a token.

Phone numbers have no checksum, so a Spanish number is reported when it has the ``+34``/``0034`` prefix, follows a label
(``tel``, ``móvil``, ``whatsapp`` …) or is written in groups (``612 345 678``, ``612 34 56 78``); nine bare digits are not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from urllib.parse import unquote

__all__ = [
    "KINDS", "Hit", "luhn_ok", "iban_ok", "dni_ok", "nie_ok", "cif_ok", "nif_ok", "ean_ok", "isbn_ok", "asin",
    "identifiers", "scan_pii", "mask_text", "mask_obj",
]

KINDS = ("DNI", "NIE", "CIF", "IBAN", "CARD", "EMAIL", "PHONE_ES")

_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
_IBAN_LENGTHS = {
    "AD": 24, "AT": 20, "BE": 16, "BG": 22, "CH": 21, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "EE": 20, "ES": 24,
    "FI": 18, "FR": 27, "GB": 22, "GI": 23, "GR": 27, "HR": 21, "HU": 28, "IE": 22, "IS": 26, "IT": 27, "LI": 21,
    "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MT": 31, "NL": 18, "NO": 15, "PL": 28, "PT": 25, "RO": 24, "SE": 24,
    "SI": 19, "SK": 24, "SM": 27,
}


def _digits(value: Any) -> str:
    return re.sub(r"[ \-]", "", "" if value is None else str(value))


def luhn_ok(value: Any) -> bool:
    """The Luhn check of a card-like number (spaces and dashes ignored; at least two digits, digits only)."""
    s = _digits(value)
    if len(s) < 2 or not re.fullmatch(r"[0-9]+", s):
        return False
    total, flip = 0, False
    for ch in reversed(s):
        d = int(ch)
        if flip:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        flip = not flip
    return total % 10 == 0


def iban_ok(value: Any) -> bool:
    """An IBAN: country, check digits, the country's exact length (15-34 for countries not in the table) and mod 97."""
    s = _digits(value).upper()
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]+", s):
        return False
    want = _IBAN_LENGTHS.get(s[:2])
    if (want is not None and len(s) != want) or (want is None and not 15 <= len(s) <= 34):
        return False
    rem = 0
    for ch in s[4:] + s[:4]:
        for d in (str(int(ch, 36)) if ch.isalpha() else ch):
            rem = (rem * 10 + int(d)) % 97
    return rem == 1


def dni_ok(value: Any) -> bool:
    """A DNI: 7-8 digits and the check letter (dots and dashes ignored)."""
    s = re.sub(r"[.\- ]", "", "" if value is None else str(value)).upper()
    m = re.fullmatch(r"([0-9]{7,8})([A-Z])", s)
    return bool(m) and _DNI_LETTERS[int(m.group(1)) % 23] == m.group(2)


def nie_ok(value: Any) -> bool:
    """A NIE: X, Y or Z, seven digits and the check letter."""
    s = re.sub(r"[.\- ]", "", "" if value is None else str(value)).upper()
    m = re.fullmatch(r"([XYZ])([0-9]{7})([A-Z])", s)
    return bool(m) and _DNI_LETTERS[int("XYZ".index(m.group(1)) * 10_000_000 + int(m.group(2))) % 23] == m.group(3)


def cif_ok(value: Any) -> bool:
    """A CIF (company tax id): entity letter, seven digits and the control character (digit for A, B, E, H; letter for
    N, P, Q, R, S, W; either for the rest)."""
    s = re.sub(r"[.\- ]", "", "" if value is None else str(value)).upper()
    m = re.fullmatch(r"([ABCDEFGHJNPQRSUVW])([0-9]{7})([0-9A-J])", s)
    if not m:
        return False
    kind, body, control = m.groups()
    total = 0
    for i, ch in enumerate(body):
        d = int(ch)
        if i % 2 == 0:                       # positions 1, 3, 5, 7: doubled, digits summed
            d *= 2
            d = d // 10 + d % 10
        total += d
    digit = (10 - total % 10) % 10
    letter = "JABCDEFGHI"[digit]
    if kind in "PQRSNW":
        return control == letter
    if kind in "ABEH":
        return control == str(digit)
    return control in (str(digit), letter)


def nif_ok(value: Any) -> bool:
    """Any Spanish tax id: DNI, NIE, CIF or the K/L/M special forms."""
    s = re.sub(r"[.\- ]", "", "" if value is None else str(value)).upper()
    if dni_ok(s) or nie_ok(s) or cif_ok(s):
        return True
    m = re.fullmatch(r"[KLM]([0-9]{7})([A-Z])", s)
    return bool(m) and _DNI_LETTERS[int(m.group(1)) % 23] == m.group(2)


def ean_ok(value: Any) -> bool:
    """A GTIN: 8 (EAN-8), 12 (UPC-A), 13 (EAN-13) or 14 (GTIN-14) digits with the GS1 check digit."""
    s = _digits(value)
    if not re.fullmatch(r"[0-9]+", s) or len(s) not in (8, 12, 13, 14):
        return False
    total = 0
    for i, ch in enumerate(reversed(s[:-1])):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - total % 10) % 10 == int(s[-1])


def isbn_ok(value: Any) -> bool:
    """An ISBN-10 (check digit may be X) or ISBN-13 (an EAN-13 starting 978 or 979)."""
    s = _digits(value).upper()
    if re.fullmatch(r"[0-9]{13}", s):
        return s[:3] in ("978", "979") and ean_ok(s)
    if re.fullmatch(r"[0-9]{9}[0-9X]", s):
        total = sum((10 - i) * (10 if ch == "X" else int(ch)) for i, ch in enumerate(s))
        return total % 11 == 0
    return False


# ---------------------------------------------------------------------------------------------- product ids
_ASIN_URL = re.compile(r"/(?:dp|gp/product|gp/aw/d|product-reviews|exec/obidos/asin)/([A-Za-z0-9]{10})(?![A-Za-z0-9])")
_ASIN_BARE = re.compile(r"(?<![A-Za-z0-9])(B0[A-Z0-9]{8})(?![A-Za-z0-9])")
_GTIN_CAND = re.compile(r"(?<![0-9])[0-9]{8,14}(?![0-9])")
_ISBN_LABEL = re.compile(r"isbn(?:-1[03])?[^0-9]{0,8}([0-9][0-9\- ]{8,16}[0-9Xx])", re.IGNORECASE)
_SKU = re.compile(r"(?:/p/|/product/|/producto/|/ip/|sku=|pid=|id=)([A-Za-z0-9_-]{5,})", re.IGNORECASE)


def asin(text: Any) -> Optional[str]:
    """The first Amazon ASIN in a text or link: the 10 characters after ``/dp/`` or ``/gp/product/`` (any case), else a
    bare upper-case ``B0xxxxxxxx``. None when there is none."""
    s = unquote("" if text is None else str(text))
    m = _ASIN_URL.search(s)
    if m:
        return m.group(1).upper()
    m = _ASIN_BARE.search(s)
    return m.group(1) if m else None


def _uniq(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for it in items:
        if it not in out:
            out.append(it)
    return out


def identifiers(text: Any, url: Any = None) -> dict[str, list[str]]:
    """Product identifiers found in ``text`` (and the link ``url``): ``{"ean": [...], "asin": [...], "isbn": [...],
    "sku": [...]}``, each in order of appearance without repeats. ``ean`` holds only digit runs of 8, 12, 13 or 14 digits
    whose GS1 check digit is right (any such run counts: a 13-digit timestamp passes one time in ten); ``isbn``
    the valid ISBN-13 among them plus labelled ISBNs; ``sku`` product ids from ``/p/…``, ``sku=…``, ``pid=…`` links
    (upper case, at least one digit)."""
    hay = unquote(f"{'' if text is None else text}\n{'' if url is None else url}")
    eans = [m.group(0) for m in _GTIN_CAND.finditer(hay) if len(m.group(0)) in (8, 12, 13, 14) and ean_ok(m.group(0))]
    isbns = [e for e in eans if len(e) == 13 and isbn_ok(e)]
    for m in _ISBN_LABEL.finditer(hay):
        cleaned = _digits(m.group(1)).upper()
        if isbn_ok(cleaned):
            isbns.append(cleaned)
    asins = [m.group(1).upper() for m in _ASIN_URL.finditer(hay)] + [m.group(1) for m in _ASIN_BARE.finditer(hay)]
    skus = [m.group(1).upper() for m in _SKU.finditer(hay) if re.search(r"[0-9]", m.group(1))]
    asin_set = set(asins)
    skus = [s for s in skus if s not in asin_set]
    return {"ean": _uniq(eans), "asin": _uniq(asins), "isbn": _uniq(isbns), "sku": _uniq(skus)}


# ---------------------------------------------------------------------------------------------- personal data
_RX_IBAN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}[0-9]{2}(?:[ -]?[A-Z0-9]{4}){3,7}(?:[ -]?[A-Z0-9]{1,4})?(?![A-Za-z0-9])")
_RX_DNI = re.compile(r"(?<![A-Za-z0-9.])[0-9]{2}\.?[0-9]{3}\.?[0-9]{3}-?[A-Za-z](?![A-Za-z0-9])")
_RX_NIE = re.compile(r"(?<![A-Za-z0-9])[XYZxyz]-?[0-9]{7}-?[A-Za-z](?![A-Za-z0-9])")
_RX_CIF = re.compile(r"(?<![A-Za-z0-9])[ABCDEFGHJNPQRSUVW]-?[0-9]{7}-?[0-9A-J](?![A-Za-z0-9])")
_RX_CARD = re.compile(r"(?<![A-Za-z0-9])(?:[0-9][ -]?){12,18}[0-9](?![A-Za-z0-9])")
_RX_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9-])")
_RX_PHONE_PREFIX = re.compile(r"(?<![A-Za-z0-9+])(?:\+34|0034)[ .-]?[6-9][0-9]{2}[ .-]?[0-9]{3}[ .-]?[0-9]{3}(?![0-9])")
_RX_PHONE_LABEL = re.compile(r"(?:tel[eé]fono|tel\.?|tfno\.?|m[oó]vil|phone|mobile|contacto|whatsapp)(?:\s*[:.]?\s*)"
                             r"((?:\+|00)?[0-9][0-9 .-]{7,16}[0-9])", re.IGNORECASE)
_RX_PHONE_GROUPED = re.compile(r"(?<![A-Za-z0-9+.-])[6-9][0-9]{2}(?:[ .-][0-9]{3}[ .-][0-9]{3}|[ .-][0-9]{2}[ .-][0-9]{2}[ .-][0-9]{2})"
                               r"(?![A-Za-z0-9-]|[.,][0-9])")


@dataclass
class Hit:
    """Personal data found in text: ``kind`` (one of :data:`KINDS`), ``start``/``end`` offsets and the matched ``value``."""
    kind: str
    start: int
    end: int
    value: str

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "start": self.start, "end": self.end, "value": self.value}


def _is_es_phone(raw: str) -> bool:
    d = re.sub(r"[^0-9]", "", raw)
    if d.startswith("0034"):
        d = d[4:]
    elif d.startswith("34") and len(d) == 11:
        d = d[2:]
    return len(d) == 9 and d[0] in "6789"


def _trim_iban(raw: str) -> str:
    """Cut a match to the country's IBAN length (the pattern may have swallowed a trailing upper-case word)."""
    clean = re.sub(r"[ -]", "", raw)
    want = _IBAN_LENGTHS.get(clean[:2])
    if want is None or len(clean) <= want:
        return raw
    seen = 0
    for i, ch in enumerate(raw):
        if ch not in " -":
            seen += 1
            if seen == want:
                return raw[:i + 1]
    return raw


def scan_pii(text: Any, kinds: Optional[Iterable[str]] = None) -> list[Hit]:
    """Personal data in ``text`` in reading order, longest match first where two overlap. Only values that pass their
    checksum count (see the module docstring for phones). ``kinds`` limits the search (default: all of :data:`KINDS`)."""
    s = "" if text is None else str(text)
    want = set(kinds) if kinds is not None else set(KINDS)
    raw: list[Hit] = []

    def add(kind: str, start: int, end: int) -> None:
        if kind in want:
            raw.append(Hit(kind, start, end, s[start:end]))

    for m in _RX_IBAN.finditer(s):
        value = _trim_iban(m.group(0))
        if iban_ok(value):
            add("IBAN", m.start(), m.start() + len(value))
    for m in _RX_DNI.finditer(s):
        if dni_ok(m.group(0)):
            add("DNI", m.start(), m.end())
    for m in _RX_NIE.finditer(s):
        if nie_ok(m.group(0)):
            add("NIE", m.start(), m.end())
    for m in _RX_CIF.finditer(s):
        if cif_ok(m.group(0)):
            add("CIF", m.start(), m.end())
    for m in _RX_CARD.finditer(s):
        digits = re.sub(r"[^0-9]", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            add("CARD", m.start(), m.end())
    for m in _RX_EMAIL.finditer(s):
        add("EMAIL", m.start(), m.end())
    for m in _RX_PHONE_PREFIX.finditer(s):
        add("PHONE_ES", m.start(), m.end())
    for m in _RX_PHONE_LABEL.finditer(s):
        if _is_es_phone(m.group(1)):
            add("PHONE_ES", m.start(1), m.end(1))
    for m in _RX_PHONE_GROUPED.finditer(s):
        add("PHONE_ES", m.start(), m.end())
    raw.sort(key=lambda h: (h.start, -(h.end - h.start)))
    out: list[Hit] = []
    for h in raw:
        if out and h.start < out[-1].end:
            continue
        out.append(h)
    return out


def mask_text(text: Any, kinds: Optional[Iterable[str]] = None, token: str = "<{kind}>") -> str:
    """``text`` with every :func:`scan_pii` hit replaced by ``token`` (``{kind}`` is replaced by the kind, so the
    default gives ``<DNI>``, ``<IBAN>`` …). Idempotent."""
    s = "" if text is None else str(text)
    out = s
    for h in reversed(scan_pii(s, kinds)):
        out = out[:h.start] + token.replace("{kind}", h.kind) + out[h.end:]
    return out


def mask_obj(obj: Any, **kw: Any) -> Any:
    """:func:`mask_text` over every string inside lists, tuples and dicts (keys stay); other values pass through."""
    if isinstance(obj, str):
        return mask_text(obj, **kw)
    if isinstance(obj, list):
        return [mask_obj(v, **kw) for v in obj]
    if isinstance(obj, tuple):
        return tuple(mask_obj(v, **kw) for v in obj)
    if isinstance(obj, dict):
        return {k: mask_obj(v, **kw) for k, v in obj.items()}
    return obj
