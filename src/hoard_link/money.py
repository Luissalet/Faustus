"""Money: parse amounts out of text, find prices in free text, format them, split and settle them.

Standard library only. The Node twin is ``js/hoard-commons/money.js`` (same names in camelCase); both are
checked against ``tests/vectors/money.json``.

Rules the whole family now shares (they used to differ app by app):

* **Decimal separators.** ``"1.234,50"`` and ``"1,234.50"`` are unambiguous: the right-most separator is the
  decimal mark. A separator that repeats (``"1.234.567"``) is a thousands mark. A lone ``"."`` or ``","`` followed by
  exactly three digits after one to three leading digits (``"1.234"``, ``"2,099"``) is **ambiguous** and is read as a
  *thousands* mark, because no common currency has three decimals; the one exception is a ``"."`` in a dot-decimal
  context (``lang="en"`` or a dot-decimal currency such as USD or GBP, from ``currency_hint`` or from the text
  itself): ``"$1.234"`` is 1.234 dollars, ``"1.234"`` with ``lang="es"`` is 1234. Everything else is decimal
  (``"12,5"``, ``"59.99"``, ``"0,123"``). ``decimal="," | "."`` forces the choice (a CSV column, see
  :func:`detect_decimal`).
* **Signs.** ``-12,50``, ``−12,50``, ``12,50-``, ``(12,50)`` are negative; the sign is never lost.
* **Currency** words and symbols (``€``, ``EUR``, ``euros``, ``US$``, ``SEK``) are accepted around a number and
  ignored by :func:`parse_amount`; :func:`find_prices` reads them to say which currency a price is in.
* **Cents.** :func:`parse_cents` and :func:`to_cents` give integers (half up). :func:`split_shares` divides cents by
  largest remainder so the parts always add up to the total; :func:`settle` gives the fewest transfers that clear
  a set of balances.

The JS side returns a ``Number`` rounded to two decimals from ``parseAmount`` (use ``parseCents`` for exact work).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

__all__ = [
    "CURRENCIES", "PriceHit", "currency_of", "parse_amount", "parse_cents", "detect_decimal", "find_prices",
    "format_money", "format_cents", "to_cents", "from_cents", "split_shares", "settle",
]

# code -> symbol, decimals, aliases (matched case-insensitively, whole words for alphabetic ones), dot-decimal flag
CURRENCIES: dict[str, dict[str, Any]] = {
    "EUR": {"symbol": "€", "decimals": 2, "name": "Euro", "dot": False, "aliases": ("€", "eur", "euro", "euros")},
    "USD": {"symbol": "$", "decimals": 2, "name": "US dollar", "dot": True,
            "aliases": ("us$", "$", "usd", "dolar", "dolares", "dólar", "dólares", "dollar", "dollars")},
    "GBP": {"symbol": "£", "decimals": 2, "name": "Pound sterling", "dot": True, "aliases": ("£", "gbp")},
    "CHF": {"symbol": "CHF", "decimals": 2, "name": "Swiss franc", "dot": True, "aliases": ("chf", "sfr")},
    "SEK": {"symbol": "SEK", "decimals": 2, "name": "Swedish krona", "dot": False, "aliases": ("sek",)},
    "DKK": {"symbol": "DKK", "decimals": 2, "name": "Danish krone", "dot": False, "aliases": ("dkk",)},
    "NOK": {"symbol": "NOK", "decimals": 2, "name": "Norwegian krone", "dot": False, "aliases": ("nok",)},
    "PLN": {"symbol": "zł", "decimals": 2, "name": "Polish zloty", "dot": False, "aliases": ("pln", "zł")},
    "CZK": {"symbol": "Kč", "decimals": 2, "name": "Czech koruna", "dot": False, "aliases": ("czk", "kč")},
    "JPY": {"symbol": "¥", "decimals": 0, "name": "Japanese yen", "dot": True, "aliases": ("jpy", "¥")},
    "MXN": {"symbol": "MX$", "decimals": 2, "name": "Mexican peso", "dot": True, "aliases": ("mxn", "mx$")},
    "CAD": {"symbol": "CA$", "decimals": 2, "name": "Canadian dollar", "dot": True, "aliases": ("cad", "ca$", "c$")},
    "AUD": {"symbol": "A$", "decimals": 2, "name": "Australian dollar", "dot": True, "aliases": ("aud", "au$", "a$")},
    "BRL": {"symbol": "R$", "decimals": 2, "name": "Brazilian real", "dot": False, "aliases": ("brl", "r$")},
    "CNY": {"symbol": "CN¥", "decimals": 2, "name": "Chinese yuan", "dot": True, "aliases": ("cny", "rmb")},
    "INR": {"symbol": "₹", "decimals": 2, "name": "Indian rupee", "dot": True, "aliases": ("inr", "₹")},
    "ARS": {"symbol": "AR$", "decimals": 2, "name": "Argentine peso", "dot": False, "aliases": ("ars",)},
    "HUF": {"symbol": "Ft", "decimals": 2, "name": "Hungarian forint", "dot": False, "aliases": ("huf",)},
    "RON": {"symbol": "lei", "decimals": 2, "name": "Romanian leu", "dot": False, "aliases": ("ron",)},
    "TRY": {"symbol": "₺", "decimals": 2, "name": "Turkish lira", "dot": False, "aliases": ("₺",)},
}

_ALIAS_TO_CODE: dict[str, str] = {}
for _code, _cur in CURRENCIES.items():
    for _alias in _cur["aliases"]:
        _ALIAS_TO_CODE[_alias.lower()] = _code


def _alias_pattern() -> str:
    alts = []
    for alias in sorted(_ALIAS_TO_CODE, key=lambda a: (-len(a), a)):
        body = re.escape(alias)
        if alias[0].isalpha():
            body = r"(?<![A-Za-z])" + body
        if alias[-1].isalpha():
            body += r"(?![A-Za-z])"
        alts.append(body)
    return "(?:" + "|".join(alts) + ")"


_CUR = _alias_pattern()
_CUR_RX = re.compile(_CUR, re.IGNORECASE)


def currency_of(marker: Any) -> str:
    """The ISO code of a currency symbol, word or code (``"€"``, ``"euros"``, ``"US$"``, ``"sek"``); ``""`` if unknown."""
    return _ALIAS_TO_CODE.get(str(marker or "").strip().lower(), "")


# ---------------------------------------------------------------------------------------------- parsing
def _strip_currency(text: str) -> tuple[str, str]:
    found: list[str] = []

    def take(m: re.Match) -> str:
        found.append(_ALIAS_TO_CODE.get(m.group(0).lower(), ""))
        return ""

    cleaned = _CUR_RX.sub(take, text)
    return cleaned, next((c for c in found if c), "")


def _dot_context(currency: str, lang: str) -> bool:
    cur = CURRENCIES.get((currency or "").upper())
    if cur is not None:
        return bool(cur["dot"])
    return str(lang or "es").lower().startswith("en")


def _number_parts(raw: str, decimal: Optional[str], dot_ctx: bool) -> Optional[tuple[bool, str, str]]:
    """``(negative, integer digits, fraction digits)`` of one cleaned amount string, or None."""
    s = re.sub(r"[\s'’]", "", raw)
    if not s:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.startswith("+"):
        s = s[1:]
    if s.startswith(("-", "−", "–")):
        neg, s = (not neg), s[1:]
    elif s.endswith("-"):
        neg, s = (not neg), s[:-1]
    if not re.fullmatch(r"[0-9.,]+", s) or not re.search(r"[0-9]", s):
        return None

    thousands: Optional[str]
    if decimal in (",", "."):
        dec: Optional[str] = decimal
        thousands = "." if decimal == "," else ","
    else:
        has_comma, has_dot = "," in s, "." in s
        dec, thousands = None, None
        if has_comma and has_dot:
            dec = "," if s.rfind(",") > s.rfind(".") else "."
            thousands = "." if dec == "," else ","
        elif has_comma or has_dot:
            sep = "," if has_comma else "."
            parts = s.split(sep)
            if len(parts) > 2:
                thousands = sep                                   # 1.234.567
            else:
                head, tail = parts
                if len(tail) == 3 and 1 <= len(head) <= 3 and not head.startswith("0"):
                    if sep == "." and dot_ctx:
                        dec = "."                                 # $1.234 -> 1.234
                    else:
                        thousands = sep                           # 1.234 / 2,099 -> 1234 / 2099
                else:
                    dec = sep                                     # 12,5 / 59.99 / 0,123
    int_raw, frac = s, ""
    if dec:
        parts = s.split(dec)
        if len(parts) > 2:
            return None
        int_raw = parts[0]
        frac = parts[1] if len(parts) == 2 else ""
    if thousands and thousands in int_raw:
        groups = int_raw.split(thousands)
        if not (1 <= len(groups[0]) <= 3) or any(len(g) != 3 for g in groups[1:]):
            return None
        int_raw = "".join(groups)
    if not re.fullmatch(r"[0-9]*", int_raw) or not re.fullmatch(r"[0-9]*", frac):
        return None
    if not int_raw and not frac:
        return None
    return neg, int_raw or "0", frac


def parse_amount(text: Any, *, decimal: Optional[str] = None, currency_hint: Optional[str] = None,
                 lang: str = "es") -> Optional[Decimal]:
    """An amount as a :class:`~decimal.Decimal` (exact, not rounded), or None when ``text`` is not a number.

    Accepts ``"1.234,56"``, ``"1,234.56"``, ``"-12,50"``, ``"(12.00)"``, ``"1 299 €"``, ``"€1,234.56"``, ``"1.599,00 SEK"``.
    Numbers and Decimals pass through. See the module docstring for how ``"1.234"`` is read; ``decimal`` forces
    the decimal mark, ``currency_hint`` (an ISO code) and ``lang`` decide the ambiguous case, and a currency written
    in ``text`` beats ``currency_hint``."""
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, Decimal):
        return text if text.is_finite() else None
    if isinstance(text, int):
        return Decimal(text)
    if isinstance(text, float):
        return Decimal(repr(text)) if math.isfinite(text) else None
    if not isinstance(text, str):
        return None
    cleaned, cur = _strip_currency(text)
    dot_ctx = _dot_context(cur or (currency_hint or ""), lang)
    parts = _number_parts(cleaned, decimal, dot_ctx)
    if parts is None:
        return None
    neg, int_part, frac = parts
    try:
        value = Decimal(f"{int_part}.{frac or '0'}")
    except InvalidOperation:
        return None
    return -value if neg else value


def to_cents(value: Any) -> Optional[int]:
    """Cents of an amount in units (``12.5`` -> 1250), rounded half up (away from zero); None if not a number."""
    d = value if isinstance(value, Decimal) else parse_amount(value)
    if d is None:
        return None
    return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_cents(cents: Any) -> Decimal:
    """The amount in units of ``cents`` (``1250`` -> ``Decimal('12.50')``)."""
    return (Decimal(int(cents)) / 100).quantize(Decimal("0.01"))


def parse_cents(text: Any, *, decimal: Optional[str] = None, currency_hint: Optional[str] = None,
                lang: str = "es") -> Optional[int]:
    """Like :func:`parse_amount` but integer cents (half up): ``"1.234,56"`` -> 123456, ``"-12,5"`` -> -1250."""
    d = parse_amount(text, decimal=decimal, currency_hint=currency_hint, lang=lang)
    return None if d is None else to_cents(d)


def detect_decimal(samples: Iterable[Any]) -> Optional[str]:
    """The decimal mark (``","`` or ``"."``) a whole column of amount strings uses, or None when no value shows it.
    Only unambiguous evidence counts: a one- or two-digit tail, or both marks in the order ``1.234,5`` / ``1,234.5``."""
    comma = dot = 0
    for raw in samples:
        v = re.sub(r"[\s]", "", _CUR_RX.sub("", "" if raw is None else str(raw)))
        if re.search(r",[0-9]{1,2}$", v):
            comma += 1
        if re.search(r"\.[0-9]{1,2}$", v):
            dot += 1
        if re.search(r"\.[0-9]{3},[0-9]+$", v):
            comma += 1
        if re.search(r",[0-9]{3}\.[0-9]+$", v):
            dot += 1
    if comma == 0 and dot == 0:
        return None
    return "," if comma >= dot else "."


# ---------------------------------------------------------------------------------------------- prices in text
_NUM = r"[0-9]{1,3}(?:[   .,'][0-9]{3})+(?:[.,][0-9]{1,2})?(?![0-9])|[0-9]+(?:[.,][0-9]{1,2})?(?![0-9])"
_SIGN = r"(?:(?<![0-9A-Za-z])([-−])[ ]?)?"
_SUFFIX = re.compile(_SIGN + r"(?<![0-9.,])(" + _NUM + r")[  ]?(" + _CUR + r")(?![0-9])", re.IGNORECASE)
_PREFIX = re.compile(_SIGN + r"(" + _CUR + r")[  ]?([-−])?(?<![0-9.,])(" + _NUM + r")", re.IGNORECASE)

_LABELS = ("importe total a pagar", "total a pagar", "importe a pagar", "importe a cargar", "importe total", "total factura",
           "total pedido", "precio total", "precio final", "amount due", "total due", "total amount", "subtotal", "total",
           "importe", "precio", "price", "amount", "cuota", "pvp", "prima", "iva", "vat", "tax", "envio", "shipping",
           "descuento", "discount", "cargo", "pagado", "paid")
_LABEL_RX = re.compile("(" + "|".join(re.escape(x) for x in sorted(_LABELS, key=lambda x: (-len(x), x))) + r")[^a-z0-9]{0,12}$")


@dataclass
class PriceHit:
    """A price found in text: ``amount`` (Decimal, negative for ``-12,50 €``), ISO ``currency``, ``start``/``end``
    offsets of the matched text, and the ``label`` that precedes it on the line (``"total"``, ``"subtotal"``…) or ""."""
    amount: Decimal
    currency: str
    start: int
    end: int
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"amount": float(self.amount), "currency": self.currency, "start": self.start, "end": self.end,
                "label": self.label}


def _fold_lower(text: str) -> str:
    from .text import fold
    return fold(text)


def _label_before(text: str, start: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    segment = _fold_lower(text[max(line_start, start - 60):start])
    m = _LABEL_RX.search(segment)
    return m.group(1) if m else ""


def find_prices(text: Any, *, labelled_only: bool = False, lang: str = "es") -> list[PriceHit]:
    """Every price written with a currency marker (before or after the number) in reading order:
    ``"1.234,56 €"``, ``"€1,234.56"``, ``"59,99€"``, ``"1 299 €"``, ``"$1,299.00"``, ``"1.599,00 SEK"``, ``"-12,50 EUR"``.
    Numbers without a currency are not prices here. ``labelled_only`` keeps those that follow a label such as
    ``Total:`` or ``Importe a pagar`` (``hit.label``)."""
    s = "" if text is None else str(text)
    raw: list[tuple[int, int, str, str, str]] = []        # start, end, sign, number, marker
    for m in _SUFFIX.finditer(s):
        raw.append((m.start(), m.end(), m.group(1) or "", m.group(2), m.group(3)))
    for m in _PREFIX.finditer(s):
        raw.append((m.start(), m.end(), m.group(1) or m.group(3) or "", m.group(4), m.group(2)))
    raw.sort(key=lambda r: (r[0], r[1]))
    out: list[PriceHit] = []
    last_end = -1
    for start, end, sign, number, marker in raw:
        if start < last_end:
            continue
        code = _ALIAS_TO_CODE.get(marker.lower(), "")
        amount = parse_amount(number, currency_hint=code, lang=lang)
        if amount is None:
            continue
        if sign:
            amount = -amount
        label = _label_before(s, start)
        if labelled_only and not label:
            continue
        out.append(PriceHit(amount, code, start, end, label))
        last_end = end
    return out


# ---------------------------------------------------------------------------------------------- formatting
_STYLE = {"es": (".", ",", False), "en": (",", ".", True), "fr": (" ", ",", False)}


def _style(lang: str) -> tuple[str, str, bool]:
    key = str(lang or "es").lower().split("-")[0]
    return _STYLE.get(key, _STYLE["es"])


def format_money(value: Any, currency: str = "EUR", lang: str = "es", *, trim_zero_cents: bool = False,
                 grouping: bool = True, nbsp: bool = False) -> str:
    """An amount (in units, not cents) as text. Spanish: ``1.234,56 €``; English: ``€1,234.56`` (currencies without
    a short symbol read ``CHF 1,234.56``). Rounded half up to the currency's decimals (JPY has none), ``-`` for
    negatives, nothing for None. ``trim_zero_cents`` drops ``,00``; ``grouping=False`` drops the thousands marks;
    ``nbsp`` uses a no-break space between number and symbol."""
    d = value if isinstance(value, Decimal) else parse_amount(value)
    if d is None:
        return ""
    cur = CURRENCIES.get(str(currency or "EUR").upper())
    decimals = cur["decimals"] if cur else 2
    symbol = cur["symbol"] if cur else str(currency or "").upper()
    q = d.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    neg = q < 0
    digits = format(abs(q), "f")
    int_part, _, frac = digits.partition(".")
    group, dec_mark, symbol_first = _style(lang)
    if grouping:
        int_part = f"{int(int_part):,}".replace(",", group)
    if trim_zero_cents and frac and not frac.strip("0"):
        frac = ""
    number = int_part + (dec_mark + frac if frac else "")
    gap = " " if nbsp else " "
    if symbol_first:
        text = (symbol + number) if symbol in ("€", "$", "£", "¥", "₹", "₺") else (symbol + gap + number)
    else:
        text = number + gap + symbol
    if not neg:
        return text
    return "-" + text


def format_cents(cents: Any, currency: str = "EUR", lang: str = "es", **kw: Any) -> str:
    """:func:`format_money` of an amount in cents (Ledger's ``formatCents``): ``123456`` -> ``1.234,56 €``."""
    if cents is None:
        return ""
    return format_money(Decimal(int(cents)) / 100, currency, lang, **kw)


# ---------------------------------------------------------------------------------------------- shares and settling
def split_shares(total_cents: int, weights: Union[Sequence[float], Mapping[Any, float]]) -> Any:
    """Divide ``total_cents`` among the ``weights`` so the parts add up exactly: each part is the floor of its exact
    share and the leftover cents go to the largest fractional parts (ties: the earlier one). A list gives a list, a
    dict a dict; a zero weight gets nothing; a negative total is split as its absolute value and negated.
    Raises ``ValueError`` when no weight is positive."""
    is_map = isinstance(weights, Mapping)
    keys = list(weights.keys()) if is_map else list(range(len(weights)))          # type: ignore[union-attr]
    ws = [float(weights[k]) for k in keys]                                          # type: ignore[index]
    if any(w < 0 for w in ws) or not any(w > 0 for w in ws):
        raise ValueError("a split needs at least one positive weight and no negative ones")
    total = int(total_cents)
    sign = -1 if total < 0 else 1
    total = abs(total)
    whole = sum(w for w in ws if w > 0)
    raw = [total * w / whole if w > 0 else 0.0 for w in ws]
    parts = [int(math.floor(v + 1e-9)) for v in raw]
    left = total - sum(parts)
    ranked = sorted((i for i, w in enumerate(ws) if w > 0), key=lambda i: (-(raw[i] - parts[i]), i))
    for i in ranked[:max(0, left)]:
        parts[i] += 1
    parts = [sign * p for p in parts]
    return dict(zip(keys, parts)) if is_map else parts


EXACT_LIMIT = 16        # settle is exact up to this many people with a balance; beyond it a greedy pass is used


def settle(balances: Mapping[str, int]) -> list[dict[str, Any]]:
    """The fewest transfers that bring every net balance to zero: ``[{"from", "to", "cents"}]`` (the debtor pays the
    creditor). ``balances`` maps a person to cents, positive when they are owed, negative when they owe; it should add
    up to zero. People are grouped into the largest number of subsets that already balance among themselves (a
    group of k people needs k-1 transfers); beyond :data:`EXACT_LIMIT` people with a balance one greedy group is used.
    Deterministic: people are taken in sorted order."""
    ids = sorted(k for k, v in balances.items() if int(v) != 0)
    net = {k: int(balances[k]) for k in ids}
    if not ids:
        return []
    groups = _groups(ids, net) if len(ids) <= EXACT_LIMIT else [ids]
    out: list[dict[str, Any]] = []
    for group in groups:
        debtors = sorted(([-net[i], i] for i in group if net[i] < 0), key=lambda x: (-x[0], x[1]))
        creditors = sorted(([net[i], i] for i in group if net[i] > 0), key=lambda x: (-x[0], x[1]))
        while debtors and creditors:
            amount = min(debtors[0][0], creditors[0][0])
            out.append({"from": debtors[0][1], "to": creditors[0][1], "cents": amount})
            debtors[0][0] -= amount
            creditors[0][0] -= amount
            if debtors[0][0] == 0:
                debtors.pop(0)
            if creditors and creditors[0][0] == 0:
                creditors.pop(0)
            debtors.sort(key=lambda x: (-x[0], x[1]))
            creditors.sort(key=lambda x: (-x[0], x[1]))
    return out


def _groups(ids: list[str], net: dict[str, int]) -> list[list[str]]:
    n = len(ids)
    full = (1 << n) - 1
    total = [0] * (1 << n)
    for mask in range(1, 1 << n):
        low = (mask & -mask).bit_length() - 1
        total[mask] = total[mask & (mask - 1)] + net[ids[low]]
    best = [0] * (1 << n)
    pick = [0] * (1 << n)
    for mask in range(1, 1 << n):
        top, choice = -1, 0
        for i in range(n):
            if mask >> i & 1:
                value = best[mask ^ (1 << i)]
                if value > top:
                    top, choice = value, i
        best[mask] = top + (1 if total[mask] == 0 else 0)
        pick[mask] = choice
    order: list[int] = []
    mask = full
    while mask:
        i = pick[mask]
        order.append(i)
        mask ^= 1 << i
    order.reverse()
    groups: list[list[str]] = []
    current: list[str] = []
    running = 0
    for i in order:
        current.append(ids[i])
        running += net[ids[i]]
        if running == 0:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups
