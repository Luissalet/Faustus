"""Tracking numbers: find them in text and links, validate them, guess the carrier, build its public tracking link.

Standard library only. The Node twin is ``js/hoard-commons/tracking.js`` (same names in camelCase) and both are
checked against ``tests/vectors/tracking.json``. Ported from Phileas's Hoard ``numbers.py``; nothing here touches
the network.

Three levels of evidence, strongest first:

1. A carrier tracking URL (``ups.com/track?tracknum=…``, ``yuntrack.com/Track/Detail/…``): the number and the carrier.
2. A self-describing format with a check digit or a fixed prefix (UPS ``1Z…`` with its mod-10 check, UPU S10
   ``RR123456785CN`` with its mod-11 check, DHL ``JJD…``, YunExpress ``YT…``, Correos ``P…`` codes).
3. A bare alphanumeric token right after a label (``número de seguimiento``, ``tracking number``) or next to a carrier
   name. Ambiguous numeric formats (DHL Express, GLS, SEUR, FedEx …) are only accepted this way.

:func:`clean_url` strips tracking parameters from a link found in a mail (what Tantalus did); it defers to
``hoard_link.web.urls.clean_url`` and only keeps a small local copy for when that module cannot be imported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, unquote_plus, urlsplit

__all__ = [
    "CARRIERS", "CARRIER_WORDS", "Tracking", "Found", "normalize", "ups_valid", "s10_valid", "classify", "plausible",
    "unwrap", "from_url", "carriers_mentioned", "find", "tracking_url", "carrier_name", "clean_url",
]

CARRIERS: dict[str, dict[str, str]] = {
    "ups": {"name": "UPS", "url": "https://www.ups.com/track?loc=es_ES&tracknum={n}"},
    "dhl": {"name": "DHL", "url": "https://www.dhl.com/es-es/home/tracking/tracking-parcel.html?submit=1&tracking-id={n}"},
    "correos": {"name": "Correos", "url": "https://www.correos.es/es/es/herramientas/localizador/envios/detalle?tracking-number={n}"},
    "correos_express": {"name": "Correos Express", "url": "https://s.correosexpress.com/SeguimientoSinCP/search?n={n}"},
    "seur": {"name": "SEUR", "url": "https://www.seur.com/livetracking/?segOnlineIdentificador={n}&segOnlineIdioma=es"},
    "gls": {"name": "GLS", "url": "https://gls-group.com/ES/es/seguimiento-envio/?match={n}"},
    "mrw": {"name": "MRW", "url": "https://www.mrw.es/seguimiento_envios/MRW_resultados_consultas.asp?modo=nacional&envio={n}"},
    "nacex": {"name": "NACEX", "url": "https://www.nacex.es/seguimientoDetalle.do?agencia_origen=&numero_albaran={n}"},
    "ctt": {"name": "CTT Express", "url": "https://www.cttexpress.com/localizador-de-envios/?sc={n}"},
    "inpost": {"name": "InPost", "url": "https://inpost.es/seguimiento-envio/?number={n}"},
    "fedex": {"name": "FedEx", "url": "https://www.fedex.com/fedextrack/?trknbr={n}"},
    "tnt": {"name": "TNT", "url": "https://www.tnt.com/express/es_es/site/herramientas-envio/seguimiento.html?searchType=con&cons={n}"},
    "yunexpress": {"name": "YunExpress", "url": "https://www.yuntrack.com/Track/Detail/{n}"},
    "cainiao": {"name": "Cainiao", "url": "https://global.cainiao.com/newDetail.htm?mailNoList={n}"},
    "postnl": {"name": "PostNL", "url": "https://jouw.postnl.nl/track-and-trace/{n}"},
    "royalmail": {"name": "Royal Mail", "url": "https://www.royalmail.com/track-your-item#/tracking-results/{n}"},
    "deutschepost": {"name": "Deutsche Post", "url": "https://www.deutschepost.de/de/s/sendungsverfolgung.html?piececode={n}"},
    "laposte": {"name": "La Poste", "url": "https://www.laposte.fr/outils/suivre-vos-envois?code={n}"},
    "chinapost": {"name": "China Post", "url": "https://t.17track.net/es#nums={n}"},
    "amazon": {"name": "Amazon", "url": ""},
    "paack": {"name": "Paack", "url": "https://paack.co/es/tracking?tracking={n}"},
    "zeleris": {"name": "Zeleris", "url": "https://www.zeleris.com/seguimiento_envio.aspx?id_seguimiento={n}"},
    "ecoscooting": {"name": "Ecoscooting", "url": "https://www.ecoscooting.com/tracking/{n}"},
    "dpd": {"name": "DPD", "url": "https://www.dpd.com/es/es/seguimiento/?parcelNumber={n}"},
    "other": {"name": "", "url": "https://t.17track.net/es#nums={n}"},
}

# Words that name a carrier in mail text; used for context-only numbers and for "Se ha enviado con UPS".
CARRIER_WORDS: list[tuple[str, str]] = [
    ("correos_express", r"correos\s*express"), ("correos", r"\bcorreos\b"), ("ups", r"\bUPS\b"), ("dhl", r"\bDHL\b"),
    ("seur", r"\bSEUR\b"), ("gls", r"\bGLS\b"), ("mrw", r"\bMRW\b"), ("nacex", r"\bNACEX\b"), ("ctt", r"\bCTT(?:\s*Express)?\b"),
    ("inpost", r"\bInPost\b|\bMondial\s+Relay\b"), ("fedex", r"\bFed\s?Ex\b"), ("tnt", r"\bTNT\b"), ("yunexpress", r"\bYun\s?Express\b"),
    ("cainiao", r"\bCainiao\b"), ("postnl", r"\bPostNL\b"), ("royalmail", r"\bRoyal\s+Mail\b"), ("deutschepost", r"\bDeutsche\s+Post\b"),
    ("paack", r"\bPaack\b"), ("zeleris", r"\bZeleris\b"), ("amazon", r"\bAmazon\s+Logistics\b"), ("ecoscooting", r"\bEcoscooting\b"),
    ("dpd", r"\bDPD\b"),
]
_CASE_SENSITIVE = ("ups", "dhl", "gls", "mrw", "tnt", "dpd")
_CARRIER_WORD_RE = [(cid, re.compile(rx, 0 if cid in _CASE_SENSITIVE else re.I)) for cid, rx in CARRIER_WORDS]

S10_COUNTRY = {"ES": "correos", "CN": "chinapost", "GB": "royalmail", "DE": "deutschepost", "NL": "postnl", "FR": "laposte"}

LABEL_RE = re.compile(
    r"(?:n[uú]mero\s+de\s+(?:seguimiento|env[ií]o|tracking)|c[oó]digo\s+de\s+(?:seguimiento|env[ií]o|recogida)|"
    r"tracking\s*(?:number|no\.?|n[º°o]|id|code)?|seguimiento|sendungsnummer|num[eé]ro\s+de\s+suivi|"
    r"localizador|n\.?\s*[ºo°]\s*de\s+env[ií]o|awb)\s*(?:es|is|:|#|\(.*?\))?\s*[:#]?\s*([A-Z0-9][A-Z0-9\- ]{6,34}[A-Z0-9])",
    re.I)
TOKEN_STOP = re.compile(r"\s{2,}|\s(?=[a-záéíóúñ]{2,})")


@dataclass
class Tracking:
    """A tracking number found in a message: the normalised ``number`` (upper case, no spaces or dashes), the ``carrier``
    id from :data:`CARRIERS` (``""`` when unknown), ``confidence`` 0-100, the ``evidence`` (``url`` | ``format`` |
    ``label`` | ``context``) and the ``url`` it came from when it was a link."""
    number: str
    carrier: str = ""
    confidence: int = 50
    evidence: str = ""
    url: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "carrier": self.carrier, "confidence": self.confidence, "evidence": self.evidence}


Found = Tracking          # Phileas's name for it


def normalize(number: Any) -> str:
    """Upper case without spaces and dashes."""
    return re.sub(r"[\s\-]", "", "" if number is None else str(number)).upper()


def ups_valid(n: Any) -> bool:
    """A UPS ``1Z`` number whose mod-10 check digit is right."""
    n = normalize(n)
    if not re.fullmatch(r"1Z[0-9A-Z]{16}", n):
        return False
    total = 0
    for i, ch in enumerate(n[2:17]):
        value = int(ch) if ch.isdigit() else (ord(ch) - 63) % 10
        total += value * 2 if i % 2 else value
    return (10 - total % 10) % 10 == int(n[17]) if n[17].isdigit() else False


def s10_valid(n: Any) -> bool:
    """A UPU S10 number (``RR123456785CN``) whose mod-11 check digit is right."""
    n = normalize(n)
    if not re.fullmatch(r"[A-Z]{2}[0-9]{9}[A-Z]{2}", n):
        return False
    weights = (8, 6, 4, 2, 3, 5, 9, 7)
    total = sum(int(d) * w for d, w in zip(n[2:10], weights))
    check = 11 - total % 11
    check = 0 if check == 10 else 5 if check == 11 else check
    return check == int(n[10])


def classify(number: Any) -> tuple[str, int]:
    """``(carrier, confidence)`` from the format alone; ``("", 0)`` when the format says nothing."""
    n = normalize(number)
    if ups_valid(n):
        return "ups", 98
    if re.fullmatch(r"1Z[0-9A-Z]{16}", n):
        return "ups", 70
    if s10_valid(n):
        return S10_COUNTRY.get(n[-2:], "other"), 92
    if re.fullmatch(r"JJD[0-9]{15,24}|JVGL[0-9]{8,20}|GM[0-9]{16,22}|00340[0-9]{15}|3S[A-Z]{4}[0-9]{6,}", n):
        return ("postnl", 85) if n.startswith("3S") else ("dhl", 88)
    if re.fullmatch(r"YT[0-9]{16}", n):
        return "yunexpress", 92
    if re.fullmatch(r"(?:LP|CN|CAINIAO)[0-9]{12,20}[A-Z]{0,2}|LP[0-9]{14}", n):
        return "cainiao", 75
    if re.fullmatch(r"TBA[0-9]{9,14}", n):
        return "amazon", 85
    if re.fullmatch(r"P[A-Z0-9]{2}[A-Z0-9]{14,20}[A-Z]?", n) and re.search(r"[0-9]{5}", n) and len(n) >= 16:
        return "correos", 72
    return "", 0


def plausible(number: Any) -> bool:
    """A token that could be a tracking number at all (not a phone, a price or a postcode)."""
    n = normalize(number)
    if not 8 <= len(n) <= 35:
        return False
    if not re.search(r"[0-9]{4}", n):
        return False
    if re.fullmatch(r"[0-9]{9}", n) and n[0] in "6789":     # Spanish phone number
        return False
    if re.fullmatch(r"(?:34)?[6789][0-9]{8}", n):
        return False
    return True


# ------------------------------------------------------------------ links
_URL_PARAMS = ("tracknum", "trackingnumber", "tracking-number", "tracking_number", "tracking-id", "trackingid", "tracking", "trknbr",
               "awb", "piececode", "match", "mailnolist", "nums", "number", "numero", "n", "sc", "envio", "segonlineidentificador",
               "numero_albaran", "id_seguimiento", "code", "cons", "shipmentnumber", "parcelnumber", "barcode", "codigo")
_URL_HOSTS = [
    ("ups", "ups.com"), ("dhl", "dhl."), ("correos_express", "correosexpress"), ("correos", "correos.es"), ("seur", "seur.com"),
    ("gls", "gls-"), ("mrw", "mrw.es"), ("nacex", "nacex"), ("ctt", "cttexpress"), ("inpost", "inpost"), ("fedex", "fedex.com"),
    ("tnt", "tnt.com"), ("yunexpress", "yuntrack"), ("yunexpress", "yunexpress"), ("cainiao", "cainiao"), ("postnl", "postnl"),
    ("royalmail", "royalmail"), ("deutschepost", "deutschepost"), ("laposte", "laposte"), ("paack", "paack"), ("zeleris", "zeleris"),
    ("other", "17track"), ("other", "parcelsapp"), ("other", "aftership"),
]
_UNWRAP_KEYS = ("U", "u", "url", "redirect", "target", "dest", "q", "link")


def unwrap(url: Any, depth: int = 3) -> str:
    """Follow click-tracking wrappers that carry the real URL inside (awstrack ``/L0/<url>``, ``?U=``, ``?url=``)."""
    url = "" if url is None else str(url)
    for _ in range(depth):
        parts = urlsplit(url)
        inner = ""
        m = (re.search(r"/L0/(https?(?::|%3A).*?)/[0-9]+/[0-9A-Za-z-]{10,}", url, re.I)
             or re.search(r"/L0/(https?(?::|%3A).*)$", url, re.I))
        if m:
            inner = unquote(m.group(1))
        else:
            qs = parse_qs(parts.query)
            for key in _UNWRAP_KEYS:
                for value in qs.get(key, []):
                    value = unquote(value)
                    if value.lower().startswith(("http://", "https://")):
                        inner = value
                        break
                if inner:
                    break
        if not inner or inner == url:
            return url
        url = inner
    return url


def from_url(url: Any) -> Optional[Tracking]:
    """The tracking number and carrier a carrier link carries (after unwrapping click trackers), or None."""
    url = unwrap(url)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    carrier = next((cid for cid, needle in _URL_HOSTS if needle in host), "")
    if not carrier:
        return None
    qs = {k.lower(): v for k, v in parse_qs(parts.query).items()}
    candidates: list[str] = []
    for key in _URL_PARAMS:
        for value in qs.get(key, []):
            candidates.extend(v for v in re.split(r"[,;\s]+", value))
    m = re.search(r"/(?:track(?:ing)?|detail|seguimiento|trace|track-and-trace)/(?:[a-z\-]+/)?([A-Za-z0-9]{8,35})(?:[/?#]|$)", parts.path, re.I)
    if m:
        candidates.append(m.group(1))
    frag = parts.fragment
    m = re.search(r"nums=([A-Za-z0-9,]+)", frag) or re.search(r"tracking-results/([A-Za-z0-9]+)", frag)
    if m:
        candidates.extend(m.group(1).split(","))
    for cand in candidates:
        n = normalize(cand)
        if plausible(n):
            fmt_carrier, _ = classify(n)
            final = fmt_carrier if carrier == "other" and fmt_carrier else carrier
            return Tracking(n, final if final != "other" else (fmt_carrier or ""), 95, "url", url=url)
    return None


# ------------------------------------------------------------------ text
def carriers_mentioned(text: Any) -> list[str]:
    """Carrier ids named in the text, in the table's order (``Correos Express`` hides plain ``Correos``)."""
    s = "" if text is None else str(text)
    seen: list[str] = []
    for cid, rx in _CARRIER_WORD_RE:
        if rx.search(s) and cid not in seen:
            if cid == "correos" and "correos_express" in seen:
                continue
            seen.append(cid)
    return seen


def _nearest_carrier(text: str, pos: int, window: int = 160) -> str:
    lo, hi = max(0, pos - window), min(len(text), pos + window)
    chunk = text[lo:hi]
    best, best_dist = "", 10 ** 9
    for cid, rx in _CARRIER_WORD_RE:
        for m in rx.finditer(chunk):
            dist = abs((lo + m.start()) - pos)
            if dist < best_dist:
                best, best_dist = cid, dist
    return best


FORMAT_RE = re.compile(
    r"\b(1Z[0-9A-Z]{16}|[A-Z]{2}[0-9]{9}[A-Z]{2}|JJD[0-9]{15,24}|JVGL[0-9]{8,20}|YT[0-9]{16}|TBA[0-9]{9,14}|00340[0-9]{15}|"
    r"P[A-Z0-9]{2}[A-Z0-9]{14,20}[A-Z]?|LP[0-9]{14})\b")


def find(text: Any, links: Optional[list[dict]] = None) -> list[Tracking]:
    """Every tracking number in a message, best evidence first, one entry per number. ``links`` is the message's
    ``[{"url": ...}]`` list (links that are not in the text itself)."""
    found: dict[str, Tracking] = {}

    def keep(item: Tracking) -> None:
        prev = found.get(item.number)
        if prev is None or item.confidence > prev.confidence:
            if prev is not None and not item.carrier:
                item.carrier = prev.carrier
            found[item.number] = item
        elif prev and not prev.carrier and item.carrier:
            prev.carrier = item.carrier

    for link in links or []:
        item = from_url(str(link.get("url") or ""))
        if item:
            keep(item)
    text = "" if text is None else str(text)
    for url in re.findall(r"https?://[^\s<>\"')\]]+", text):
        item = from_url(url)
        if item:
            keep(item)
    for m in FORMAT_RE.finditer(text):
        n = normalize(m.group(1))
        carrier, conf = classify(n)
        if not carrier or conf < 70:
            continue
        if carrier == "correos" and conf < 90 and not re.search(r"correos|recogida|seguimiento|env[ií]o", text, re.I):
            continue
        near = _nearest_carrier(text, m.start())
        if near and carrier in ("other", ""):
            carrier = near
        keep(Tracking(n, carrier, conf, "format"))
    for m in LABEL_RE.finditer(text):
        raw = TOKEN_STOP.split(m.group(1))[0].strip()
        first = raw.split(" ")[0]
        # "1Z… y RR…": a complete number before a space wins over gluing the words together
        if " " in raw and (classify(first)[0] or (plausible(first) and len(normalize(first)) >= 10)):
            raw = first
        n = normalize(raw)
        if not plausible(n) or re.fullmatch(r"[A-Z]+", n):
            continue
        if re.fullmatch(r"[0-9]{3}-?[0-9]{7}-?[0-9]{7}", raw.replace(" ", "")):    # an Amazon order number, not a parcel
            continue
        carrier, conf = classify(n)
        near = _nearest_carrier(text, m.start())
        keep(Tracking(n, carrier or near, max(conf, 80 if near else 65), "label"))
    return sorted(found.values(), key=lambda f: -f.confidence)


def tracking_url(carrier: Any, number: Any) -> str:
    """The carrier's public tracking page for ``number``; ``""`` when the carrier has none (Amazon) or is unknown."""
    template = (CARRIERS.get(str(carrier or "")) or {}).get("url") or ""
    return template.format(n=normalize(number)) if template and number else ""


def carrier_name(carrier: Any) -> str:
    """Display name of a carrier id (``"correos_express"`` -> ``"Correos Express"``); the upper-cased id when unknown."""
    cid = str(carrier or "")
    return (CARRIERS.get(cid) or {}).get("name") or cid.upper()


# ------------------------------------------------------------------ mail links
# local copy of hoard_link.web.urls' lists, used only when that module cannot be imported
_TRACK_PREFIXES = ("utm_", "mc_", "_hs", "vero_", "trk")
_TRACK_EXACT = frozenset({
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref_src", "ref_url", "ref_", "yclid", "twclid",
    "wbraid", "gbraid", "ttclid", "li_fat_id", "srsltid", "_gl", "_ga", "_hsenc", "_hsmi", "vero_id", "vero_conv", "s_cid", "spm",
    "snr", "ser", "eid", "c2id", "mkt_tok", "trackingid", "refid", "e", "cid", "goal"})
_LOCAL_REDIRECT_KEYS = ("url", "u", "redirect", "redirect_url", "redirecturl", "link", "target", "dest", "destination")


def _unwrap_awstrack(url: str) -> str:
    m = (re.search(r"/L0/(https?(?::|%3A).*?)/[0-9]+/[0-9A-Za-z-]{10,}", url, re.I) or re.search(r"/L0/(https?(?::|%3A).*)$", url, re.I))
    return unquote(m.group(1)) if m else url


def _local_clean_url(url: str) -> str:
    """Minimal stand-in used only when ``hoard_link.web.urls`` cannot be imported: unwraps ``?url=https://…`` redirects,
    drops tracking parameters and the fragment, keeps every other query token exactly as written."""
    for _ in range(3):
        parts = urlsplit(url)
        inner = ""
        for token in parts.query.split("&"):
            key, _, value = token.partition("=")
            if unquote_plus(key).lower() in _LOCAL_REDIRECT_KEYS and unquote(value).strip().lower().startswith(("http://", "https://")):
                inner = unquote(value).strip()
                break
        if not inner:
            break
        url = inner
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url
    keep = []
    for token in parts.query.split("&"):
        if not token:
            continue
        name = unquote_plus(token.partition("=")[0]).lower()
        if name in _TRACK_EXACT or name.startswith(_TRACK_PREFIXES):
            continue
        keep.append(token)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" + ("?" + "&".join(keep) if keep else "")


def clean_url(url: Any) -> str:
    """A link from a mail without its click-tracking wrapper (awstrack ``/L0/…``, ``?url=…``), tracking parameters
    (``utm_*``, ``gclid``, ``mc_eid``, ``e``, ``cid`` …) and fragment; the rest is kept as written. Defers to
    ``hoard_link.web.urls.clean_url(mail=True)``."""
    s = _unwrap_awstrack("" if url is None else str(url).strip())
    try:
        from .web.urls import clean_url as web_clean
    except Exception:       # noqa: BLE001 — the commons never fail at import time
        return _local_clean_url(s)
    return web_clean(s, mail=True)
