"""Merchants: who a name, a web domain or a mail sender is, whether two names are the same shop, and order references.

Standard library only. The Node twin is ``js/hoard-commons/merchants.js`` (same names in camelCase); both read the
same data file (``_data/merchants.json`` here, ``merchants.json`` next to the JS) and are checked against
``tests/vectors/merchants_cases.json``.

The data file holds ~190 merchants (streaming, cloud, telecom, utilities, insurance, transport, food, shops, banks,
carriers...) and the tables around them. Every entry has ``id``, ``display``, ``domains``, ``aliases`` (folded),
``patterns`` (folded lower-case regexes valid in Python and JavaScript), ``category``, the flags ``subscription`` /
``carrier`` / ``bank`` / ``gateway`` and, for some, ``tracking_id`` (the key of :data:`hoard_link.tracking.CARRIERS`)
and ``senders``. **The order of the list is a priority**: an entry that lives inside another platform comes first
(AWS before Amazon, Correos Express before Correos, Amazon Prime before Amazon).

:func:`lookup` resolves its argument in this order: an exact sender address, a domain (the longest known domain wins,
subdomains count), the exact folded name or an alias, then the patterns. Two names are "the same merchant"
(:func:`merchant_similar`) when they resolve to the same entry; when they do not both resolve, when they share a
significant word or one word of four letters or more sits inside the other name.

This replaces the merchant tables of Ledger (``mail-merchants.js``, ``mail-match.js``, ``recurring.js``), Tantalus
(subscription vendors), Phileas (carrier and shop names) and the order-number patterns of Ledger and Phileas.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .text import fold
from .tracking import ups_valid

__all__ = [
    "DATA", "MERCHANTS", "lookup", "merchant_id", "merchant_key", "plain_key", "merchant_tokens", "merchant_similar",
    "category_of", "category_hints", "carrier_of", "is_bank", "is_subscription", "is_carrier", "is_gateway",
    "is_noise_sender", "find_merchants", "order_key", "find_order_refs", "find_order_ref",
]

DATA: dict[str, Any] = json.loads((Path(__file__).resolve().parent / "_data" / "merchants.json").read_text(encoding="utf-8"))
MERCHANTS: list[dict[str, Any]] = DATA["merchants"]

_SUFFIXES = frozenset(DATA["legal_suffixes"])
_STOP = frozenset(DATA["stop_tokens"])
_BANK_RX = re.compile(DATA["bank_pattern"])
_NOISE = tuple(DATA["noise_senders"])


def _prep(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "m": m,
        "patterns": [re.compile(p) for p in m["patterns"]],
        "names": {plain_key_raw(m["display"]), *(plain_key_raw(a) for a in m["aliases"])} - {""},
        "words": [re.compile(r"\b" + re.escape(plain_key_raw(a)) + r"\b") for a in m["aliases"] if len(a) >= 5],
        "domains": [d.lower() for d in m["domains"]],
        "senders": [s.lower() for s in m.get("senders", [])],
    }


_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_DIGITS = re.compile(r"^[0-9]+$")


def plain_key_raw(text: Any) -> str:
    """Folded, non-alphanumerics collapsed to single spaces, trimmed (no legal suffix handling)."""
    return _NON_ALNUM.sub(" ", fold(text, keep_length=False)).strip()


_DOTTED_1 = re.compile(r"\b([a-z])\.\s?(?=[a-z]\.)")
_DOTTED_2 = re.compile(r"\b([a-z])\.")


def plain_key(name: Any, *, strip_legal: bool = True) -> str:
    """Stable key of a name: folded, punctuation collapsed to single spaces, and (unless ``strip_legal=False``) the
    legal suffix removed (``S.L.``, ``SAU``, ``Ltd``, ``GmbH``...). ``"PC COMPONENTES SL"`` -> ``"pc componentes"``."""
    s = fold(name, keep_length=False)
    if strip_legal:
        s = _DOTTED_2.sub(r"\1", _DOTTED_1.sub(r"\1", s))
    words = _NON_ALNUM.sub(" ", s).split()
    if strip_legal:
        while len(words) > 1 and words[-1] in _SUFFIXES:
            words.pop()
    return " ".join(words)


_PREP = [_prep(m) for m in MERCHANTS]
_ADDR = re.compile(r"<([^<>\s]+@[^<>\s]+)>|([^\s<>,;\"']+@[^\s<>,;\"']+)")
_HOST = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^/@\s]*@)?([a-z0-9][a-z0-9.-]*\.[a-z]{2,})(?::[0-9]+)?(?:[/?#].*)?$")


def _host_of(text: str) -> str:
    m = _HOST.match(text.strip().lower())
    return m.group(1) if m else ""


def _by_sender(address: str) -> Optional[dict[str, Any]]:
    for p in _PREP:
        if address in p["senders"]:
            return p["m"]
    return None


def _by_domain(host: str) -> Optional[dict[str, Any]]:
    best, size = None, 0
    for p in _PREP:
        for d in p["domains"]:
            if len(d) > size and (host == d or host.endswith("." + d)):
                best, size = p["m"], len(d)
    return best


def _copy(m: dict[str, Any]) -> dict[str, Any]:
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in m.items()}
    out["key"] = m["id"]
    return out


def _resolve(value: Any) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    low = text.lower()
    found = _ADDR.search(low)
    if found:
        address = (found.group(1) or found.group(2)).strip("<>")
        hit = _by_sender(address) or _by_domain(address.rsplit("@", 1)[1])
        if hit:
            return hit
    else:
        host = _host_of(low)
        if host:
            hit = _by_domain(host)
            if hit:
                return hit
    key = plain_key(text)
    if not key:
        return None
    for p in _PREP:
        if key in p["names"]:
            return p["m"]
    folded = " ".join(fold(text, keep_length=False).split())
    spaced = _NON_ALNUM.sub(" ", folded).strip()
    for p in _PREP:
        if any(rx.search(folded) for rx in p["patterns"]) or any(rx.search(spaced) for rx in p["words"]):
            return p["m"]
    return None


def lookup(name_or_domain_or_sender: Any) -> Optional[dict[str, Any]]:
    """The merchant a name (``"AMZN Mktp ES"``), a domain / URL (``"www.netflix.com"``) or a mail sender
    (``"Netflix <info@mailer.netflix.com>"``) belongs to, as a copy of its data entry plus ``key`` (= ``id``);
    ``None`` when unknown."""
    hit = _resolve(name_or_domain_or_sender)
    return _copy(hit) if hit else None


def merchant_id(value: Any) -> Optional[str]:
    """``lookup(value)["id"]`` or ``None``."""
    hit = _resolve(value)
    return hit["id"] if hit else None


def merchant_key(name: Any) -> str:
    """Stable grouping key: the id of the known merchant, else :func:`plain_key` (legal suffix removed)."""
    return merchant_id(name) or plain_key(name)


def merchant_tokens(text: Any) -> list[str]:
    """Significant lower-case words of a merchant or bank description (3+ characters, not a stop word or a number),
    in order of appearance without repeats."""
    seen: list[str] = []
    for t in _NON_ALNUM.sub(" ", fold(text, keep_length=False)).split():
        if len(t) >= 3 and t not in _STOP and not _DIGITS.match(t) and t not in seen:
            seen.append(t)
    return seen


def merchant_similar(a: Any, b: Any) -> bool:
    """Do two names plausibly denote the same merchant? Known merchants compare by id (so "Amazon Prime" is not
    "Amazon Web Services"). Otherwise by significant words: the words of one are all in the other, or at least half of
    all the words are shared (so "Bar Manolo" and "Bar Pepe" differ), or a word of 4+ letters is glued into another
    (``"NETFLIXCOM"`` / ``"Netflix Billing"``)."""
    if not a or not b:
        return False
    ka, kb = merchant_id(a), merchant_id(b)
    if ka and kb:
        return ka == kb
    ta, tb = merchant_tokens(a), merchant_tokens(b)
    if not ta or not tb:
        return False
    sa, sb = set(ta), set(tb)
    if sa <= sb or sb <= sa:
        return True
    if len(sa & sb) / len(sa | sb) >= 0.5:
        return True
    # one word glued onto another: "netflix" / "netflixcom"
    return any(t != u and min(len(t), len(u)) >= 4 and (t in u or u in t) for t in ta for u in tb)


def category_of(name: Any) -> Optional[str]:
    """Category id of the known merchant (``"streaming"``, ``"telecom"``, ``"shopping"``...) or ``None``."""
    hit = _resolve(name)
    return hit["category"] if hit else None


def category_hints(category: Any) -> list[str]:
    """Names (Spanish) an app's own categories commonly have for a merchant category, best first, to pick one of the
    user's categories: ``category_hints("telecom")`` -> ``["Telefonía", "Teléfono", ...]``."""
    return list(DATA["category_hints"].get(str(category or ""), []))


def carrier_of(name: Any) -> Optional[str]:
    """The :data:`hoard_link.tracking.CARRIERS` key of a carrier merchant (``"correos_express"``), else ``None``."""
    hit = _resolve(name)
    return hit.get("tracking_id") if hit and hit["carrier"] else None


def _flag(name: Any, flag: str) -> bool:
    hit = _resolve(name)
    return bool(hit and hit[flag])


def is_subscription(name: Any) -> bool:
    return _flag(name, "subscription")


def is_carrier(name: Any) -> bool:
    return _flag(name, "carrier")


def is_gateway(name: Any) -> bool:
    return _flag(name, "gateway")


def is_bank(name: Any) -> bool:
    """A bank, card network or fintech: a known bank entry or a bank word in the name."""
    if not name:
        return False
    return _flag(name, "bank") or bool(_BANK_RX.search(" ".join(fold(name, keep_length=False).split())))


def is_noise_sender(sender: Any) -> bool:
    """A sender whose mail is marketing or a notification, never a receipt (newsletters, delivery apps, social networks)."""
    s = fold(sender, keep_length=False)
    return any(n in s for n in _NOISE)


def find_merchants(text: Any) -> list[dict[str, Any]]:
    """Every known merchant named in a text, in order of appearance, as ``{"id", "start", "end"}`` (offsets in
    ``text``). A merchant whose mention sits inside a longer one is dropped ("Amazon" inside "Amazon Web Services")."""
    folded = fold(text)
    spans: list[tuple[int, int, str]] = []
    for p in _PREP:
        best = None
        for rx in p["patterns"]:
            m = rx.search(folded)
            if m and m.end() > m.start() and (best is None or m.start() < best[0]):
                best = (m.start(), m.end())
        if best:
            spans.append((best[0], best[1], p["m"]["id"]))
    keep = [s for s in spans if not any(o is not s and o[0] <= s[0] and s[1] <= o[1] and (o[1] - o[0]) > (s[1] - s[0]) for o in spans)]
    keep.sort(key=lambda s: (s[0], s[1]))
    return [{"id": i, "start": a, "end": b} for a, b, i in keep]


# ------------------------------------------------------------------------------------------------ order references
_REF = r"([a-z0-9][a-z0-9./-]{2,34}[a-z0-9])"
_REF_PATTERNS = [
    re.compile(r"\b([a-z]?[0-9]{2,3}-[0-9]{7}-[0-9]{7})\b"),                       # Amazon order numbers
    re.compile(r"\b(gs\.[0-9]{4}-[0-9]{4}-[0-9]{4})\b"),                           # Google Store
    re.compile(r"\b((?:sop|gpa)\.[0-9.-]{8,40})"),                                 # Google Play
    re.compile(r"(?:id de pedido|order id|order #|pedido #|invoice #|factura #|n[º°o]\.? de factura|referencia del pedido)\s*[:#]?\s*" + _REF),
    re.compile(r"(?:numero|num\.?|n\.?[º°o]\.?)\s*(?:de\s+)?(?:pedido|orden|order|factura|invoice|recibo|receipt)\s*(?:es|is|:|#)?\s*[:#]?\s*" + _REF),
    re.compile(r"\b(?:pedido|orden|order)\s*(?:n\.?[º°o]\.?|no\.?|num\.?|number|numero|#|:|es|is)\s*[:#]?\s*" + _REF),
    re.compile(r"\b(?:pedido|order)\s+#?([0-9]{5,})\b"),
    re.compile(r"(?:bestellung|commande)\s*(?:number|no\.?|nr\.?|n[º°o]|#|nummer|numero)?\s*[:#]?\s*([a-z]{0,3}[0-9][a-z0-9-]{4,24})"),
    re.compile(r"(?:factura|invoice|recibo|receipt|referencia|reference|ref\.?)\s*(?:no\.?|n[º°o]\.?|#|:)?\s*[:#]?\s*" + _REF),
]
_DATE_LIKE = (re.compile(r"^[0-9]{1,2}[-/.][0-9]{1,2}[-/.][0-9]{2,4}$"), re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"))
_TRIM = re.compile(r"[-/.]+$")


def _valid_ref(ref: str) -> bool:
    return (sum(c in "0123456789" for c in ref) >= 3 and not any(rx.match(ref) for rx in _DATE_LIKE) and not ups_valid(ref))


def order_key(ref: Any) -> str:
    """Comparison form of an order or invoice reference: upper case, letters and digits only
    (``"123-1234567-7654321"`` and ``"123 1234567 7654321"`` agree, so do ``"#123-456"`` and ``"123456"``); ``""`` when
    fewer than four characters are left (too short to trust)."""
    key = _NON_ALNUM.sub("", fold(ref, keep_length=False)).upper()
    return key if len(key) >= 4 else ""


def find_order_refs(text: Any, *, limit: int = 8) -> list[str]:
    """Order, invoice and receipt references in a text (subject and body together), upper case and as written
    (``"123-1234567-7654321"``, ``"GS.1111-2222-3333"``, ``"AB-554433"``), best evidence first: Amazon ``3-7-7``,
    Google Store, Google Play, then the text after a label (``pedido n.º``, ``order #``, ``Bestellung``, ``commande``,
    ``factura``, ``ref.``). Dates, UPS tracking numbers and anything with fewer than three digits are skipped; a
    reference found by two patterns is listed once (compared with :func:`order_key`)."""
    folded = fold(text)
    out: list[str] = []
    seen: set[str] = set()
    for rx in _REF_PATTERNS:
        for m in rx.finditer(folded):
            ref = _TRIM.sub("", m.group(1))
            if len(ref) < 4 or not _valid_ref(ref):
                continue
            ref = ref.upper()[:40]
            k = _NON_ALNUM.sub("", ref.lower())
            if k in seen:
                continue
            seen.add(k)
            out.append(ref)
            if len(out) >= limit:
                return out
    return out


def find_order_ref(text: Any) -> str:
    """The first of :func:`find_order_refs`, or ``""``."""
    refs = find_order_refs(text, limit=1)
    return refs[0] if refs else ""
