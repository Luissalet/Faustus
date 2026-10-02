"""URL identity: one normaliser, one tracking list, one registrable-domain table.

The Node twin is ``js/hoard-commons/web.js`` (same names in camelCase); both are checked against
``tests/vectors/web_urls.json``.

* :func:`normalize_url` — the canonical form used as identity: scheme and host lowercased, default port,
  credentials, fragment and tracking parameters dropped, parameters sorted, no trailing slash. Returns ``""``
  for anything that is not an ``http(s)`` URL with a host.
* :func:`url_key` — identity for merging and de-duplicating: like :func:`normalize_url` but ignoring the scheme
  and ``www.``.
* :func:`unwrap_redirect` / :func:`clean_url` — read the destination of a tracking redirect that carries it in a
  parameter, and strip tracking from a link without reordering the rest (what a mail parser wants).
* :func:`host_of`, :func:`registrable_domain` — the host of a URL and its registrable domain (``foo.gob.es``
  is its own domain, ``a.b.co.uk`` is ``b.co.uk``).

Choices worth knowing

* ``ref`` is **not** a tracking parameter by default: on code hosts and wikis it selects the content. It is dropped
  only on the few hosts in :data:`REF_TRACKING_HOSTS`, or everywhere with ``strip_ref=True`` (what the old Links
  normaliser did). ``ref_src``, ``ref_url`` and Amazon's ``ref_`` are always dropped.
* ``www.`` is kept by :func:`normalize_url` (a few hosts really serve different content there) and dropped only
  by :func:`url_key` or ``strip_www=True``.
* Parameters are compared and sorted as written (no decode and re-encode), so a normalised URL never changes
  bytes it did not have to.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Any, Callable, Iterable, NamedTuple, Optional
from urllib.parse import unquote, unquote_plus

__all__ = [
    "TRACKING_PARAMS", "TRACKING_PREFIXES", "MAIL_TRACKING_PARAMS", "REF_TRACKING_HOSTS", "REDIRECT_KEYS",
    "SITE_RULES", "PUBLIC_SUFFIXES", "UrlParts", "split_url", "clean_host",
    "normalize_url", "url_key", "unwrap_redirect", "clean_url", "host_of", "registrable_domain", "is_tracking_param",
]

# One list: Faustus research citations + Tantalus search and mail + Links + JobHunters. Compared lowercase.
TRACKING_PARAMS: frozenset[str] = frozenset({
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref_src", "ref_url", "ref_",
    "yclid", "twclid", "wbraid", "gbraid", "ttclid", "li_fat_id", "srsltid", "_gl", "_ga",
    "_hsenc", "_hsmi", "vero_id", "vero_conv", "s_cid", "spm", "snr", "ser", "eid", "c2id", "mkt_tok",
    "trackingid", "refid",
})
TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "mc_", "_hs", "vero_", "trk")
# Short names that are tracking in a mail but meaningful elsewhere: only dropped with ``clean_url(mail=True)``.
MAIL_TRACKING_PARAMS: frozenset[str] = frozenset({"e", "cid", "goal"})
REF_TRACKING_HOSTS: tuple[str, ...] = ("producthunt.com", "etsy.com", "medium.com", "indiehackers.com")

# Keys that carry the real destination of a tracking redirect. ``q``, ``to`` and ``r`` are also ordinary parameters
# (``/search?q=https://x``), so they only count on a path that looks like a redirector (``/url``, ``/redirect`` …).
REDIRECT_KEYS: tuple[str, ...] = ("url", "u", "redirect", "redirect_url", "redirecturl", "link", "target", "dest",
                                  "destination", "to", "r", "q")
_AMBIGUOUS_REDIRECT_KEYS = frozenset({"to", "r", "q"})
_REDIRECT_PATH = re.compile(
    r"(?:^|/)(?:url|redirect|redir|r|l|out|click|clk|away|go|goto|ck|track|tracking|link|exit|ext|jump)(?:\.php|\.aspx?)?(?:/|$)")

_DEFAULT_PORTS = {"http": "80", "https": "443"}

_URL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://([^/?#]*)([^?#]*)(?:\?([^#]*))?(?:#(.*))?$", re.S)
_BARE_HOST = re.compile(r"^(?:[A-Za-z0-9¡-￿](?:[A-Za-z0-9¡-￿\-]*[A-Za-z0-9¡-￿])?\.)+[A-Za-z¡-￿]{2,}(?::\d+)?(?:[/?#]|$)")


class UrlParts(NamedTuple):
    scheme: str       # lowercase
    userinfo: str
    host: str         # as written (IPv6 without brackets)
    port: str         # digits as written, "" when absent
    path: str
    query: str        # without "?", "" when absent
    fragment: str     # without "#"
    has_query: bool


def split_url(url: Any) -> Optional[UrlParts]:
    """Split ``scheme://[userinfo@]host[:port]/path?query#fragment`` (hierarchical URLs only). ``None`` when the
    text is not one. The Node twin uses the same rules, so both languages agree byte for byte."""
    m = _URL_RE.match("" if url is None else str(url).strip())
    if not m:
        return None
    scheme, authority, path, query, fragment = m.groups()
    userinfo = ""
    if "@" in authority:
        userinfo, authority = authority.rsplit("@", 1)
    host, port = authority, ""
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            return None
        host = authority[1:end]
        rest = authority[end + 1:]
        if rest:
            if not rest.startswith(":") or not (rest[1:] == "" or rest[1:].isdigit()):
                return None
            port = rest[1:]
    elif ":" in authority:
        host, _, tail = authority.rpartition(":")
        if tail == "" or tail.isdigit():
            port = tail
        else:
            return None
    return UrlParts(scheme.lower(), userinfo, host, port, path or "", query or "", fragment or "", query is not None)


def clean_host(host: str) -> str:
    """Lowercase, NFKC, no trailing dot, internationalised names in punycode. ``""`` when it cannot be encoded."""
    h = unicodedata.normalize("NFKC", host or "").strip().lower().rstrip(".")
    if not h:
        return ""
    if h.isascii():
        return h
    try:
        return h.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def is_tracking_param(name: str, *, extra: Iterable[str] = ()) -> bool:
    n = unquote_plus(name).lower()
    return n in TRACKING_PARAMS or n.startswith(TRACKING_PREFIXES) or n in {e.lower() for e in extra}


# ---- site rules -------------------------------------------------------------------------------

_LINKEDIN_VIEW = re.compile(r"/jobs/view/(?:[^/]*-)?(\d+)")


def _linkedin_job(host: str, parts: UrlParts) -> Optional[str]:
    """Every LinkedIn job link (slugged, with tracking, from a collection page) is one job: /jobs/view/<id>."""
    m = _LINKEDIN_VIEW.search(parts.path)
    if not m:
        for pair in parts.query.split("&"):
            k, _, v = pair.partition("=")
            if k == "currentJobId" and v.isdigit():
                m = re.match(r"(\d+)", v)
                break
    return f"https://www.linkedin.com/jobs/view/{m.group(1)}" if m else None


# host suffix -> rule(host, parts) returning a replacement URL (itself normalised afterwards) or None
SITE_RULES: dict[str, Callable[[str, UrlParts], Optional[str]]] = {
    "linkedin.com": _linkedin_job,
}


def _site_rule(host: str, rules: dict[str, Callable[[str, UrlParts], Optional[str]]]) -> Optional[Callable]:
    for suffix, rule in rules.items():
        if host == suffix or host.endswith("." + suffix):
            return rule
    return None


# ---- normalisation ----------------------------------------------------------------------------

def _prepare(url: Any) -> str:
    s = "" if url is None else str(url).strip()
    if s.startswith("//"):
        return "https:" + s
    if "://" not in s and _BARE_HOST.match(s):
        return "https://" + s
    return s


def _ref_stripped(host: str, strip_ref: bool) -> bool:
    return strip_ref or any(host == h or host.endswith("." + h) for h in REF_TRACKING_HOSTS)


def _query_tokens(query: str, host: str, *, strip_ref: bool, drop: set[str], keep: set[str]) -> list[str]:
    out = []
    for token in query.split("&"):
        if not token:
            continue
        name = unquote_plus(token.partition("=")[0]).lower()
        if name in keep:
            out.append(token)
            continue
        if name in drop or is_tracking_param(name) or (name == "ref" and _ref_stripped(host, strip_ref)):
            continue
        out.append(token)
    return out


def _sort_key(token: str) -> tuple[str, str]:
    return (unquote_plus(token.partition("=")[0]), token)


def normalize_url(url: Any, *, strip_www: bool = False, drop_params: Iterable[str] = (), keep_params: Iterable[str] = (),
                  site_rules: Any = True, strip_ref: bool = False, keep_fragment: bool = False,
                  sort_params: bool = True) -> str:
    """Canonical identity of a page (see the module docstring). ``""`` when ``url`` is not an http(s) URL.

    ``drop_params`` adds parameter names to drop, ``keep_params`` protects names that would otherwise be dropped.
    ``site_rules`` is ``True`` (use :data:`SITE_RULES`), ``False``, or a dict of your own."""
    p = split_url(_prepare(url))
    if p is None or p.scheme not in ("http", "https"):
        return ""
    host = clean_host(p.host)
    if not host:
        return ""
    if p.port and (not p.port.isdigit() or int(p.port) > 65535):
        return ""
    rules = SITE_RULES if site_rules is True else (site_rules or {})
    if rules:
        rule = _site_rule(host, rules)
        replacement = rule(host, p) if rule else None
        if replacement:
            return normalize_url(replacement, strip_www=strip_www, site_rules=False, keep_fragment=keep_fragment)
    if strip_www and host.startswith("www."):
        host = host[4:]
    port = str(int(p.port)) if p.port else ""
    if port == _DEFAULT_PORTS[p.scheme]:
        port = ""
    shown = f"[{host}]" if ":" in host else host
    netloc = shown + (":" + port if port else "")
    path = p.path
    if len(path) > 1:
        path = path.rstrip("/")
    if path in ("/", ""):
        path = ""
    tokens = _query_tokens(p.query, host, strip_ref=strip_ref, drop={d.lower() for d in drop_params},
                           keep={k.lower() for k in keep_params})
    if sort_params:
        tokens.sort(key=_sort_key)
    out = f"{p.scheme}://{netloc}{path}"
    if tokens:
        out += "?" + "&".join(tokens)
    if keep_fragment and p.fragment:
        out += "#" + p.fragment
    return out


def url_key(url: Any, **opts: Any) -> str:
    """``host/path?query`` of the normalised URL, ignoring the scheme and ``www.`` — the merge key for results of
    different engines and links saved with and without ``www``. ``""`` for a URL that does not normalise."""
    norm = normalize_url(url, strip_www=True, **opts)
    return norm.split("://", 1)[1] if norm else ""


def host_of(url: Any) -> str:
    """Lowercase host of a URL (no port, userinfo or brackets, ``www.`` kept). ``""`` when there is none."""
    p = split_url(_prepare(url))
    return clean_host(p.host) if p else ""


# ---- redirects and mail links -----------------------------------------------------------------

def _qs_pairs(query: str) -> list[tuple[str, str]]:
    out = []
    for token in query.split("&"):
        if token:
            k, _, v = token.partition("=")
            out.append((unquote_plus(k), unquote_plus(v)))
    return out


def unwrap_redirect(url: Any, *, max_hops: int = 3) -> str:
    """The destination of a tracking redirect that carries it as a parameter (never fetched, only read from the
    text): ``https://t.example/c?url=https%3A%2F%2Fshop.example%2Fa`` -> ``https://shop.example/a``. The inner
    value must itself be an absolute http(s) URL; ``q``, ``to`` and ``r`` only count on a redirector-looking path."""
    cur = "" if url is None else str(url).strip()
    for _ in range(max(0, max_hops)):
        p = split_url(cur)
        if p is None or not p.query:
            break
        looks_like_redirector = bool(_REDIRECT_PATH.search(p.path.lower()))
        pairs = _qs_pairs(p.query)
        inner = ""
        for key in REDIRECT_KEYS:
            if key in _AMBIGUOUS_REDIRECT_KEYS and not looks_like_redirector:
                continue
            for k, v in pairs:
                if k.lower() == key:
                    candidate = unquote(v).strip()
                    if candidate.lower().startswith(("http://", "https://")):
                        inner = candidate
                        break
            if inner:
                break
        if not inner:
            break
        cur = inner
    return cur


def clean_url(url: Any, *, mail: bool = False, keep_fragment: bool = False, max_len: int = 0) -> str:
    """Unwrap a redirect link and drop tracking parameters and the fragment, keeping everything else exactly as
    written (order included). ``mail=True`` also drops the short names mail systems use (``e``, ``cid``, ``goal``).
    ``max_len`` truncates the result (0 = no limit)."""
    cur = unwrap_redirect(url)
    p = split_url(cur)
    if p is None:
        return cur[:max_len] if max_len else cur
    host = clean_host(p.host)
    extra = MAIL_TRACKING_PARAMS if mail else frozenset()
    keep = []
    for token in p.query.split("&"):
        if not token:
            continue
        name = unquote_plus(token.partition("=")[0]).lower()
        if name in extra or is_tracking_param(name) or (name == "ref" and _ref_stripped(host, False)):
            continue
        keep.append(token)
    # rebuild from the original text so nothing but the dropped parts changes
    authority = (p.userinfo + "@" if p.userinfo else "") + (f"[{p.host}]" if ":" in p.host else p.host) + (":" + p.port if p.port else "")
    out = f"{p.scheme}://{authority}{p.path}"
    if keep:
        out += "?" + "&".join(keep)
    if keep_fragment and p.fragment:
        out += "#" + p.fragment
    return out[:max_len] if max_len else out


# ---- registrable domain -----------------------------------------------------------------------

def _suffixes(spec: str) -> frozenset[str]:
    return frozenset(spec.split())


# Multi-label public suffixes only (single-label TLDs need no entry). Not the full list: the ones the family meets.
PUBLIC_SUFFIXES: frozenset[str] = _suffixes("""
com.es org.es nom.es gob.es edu.es
co.uk org.uk gov.uk ac.uk me.uk ltd.uk plc.uk net.uk sch.uk nhs.uk police.uk
com.au net.au org.au edu.au gov.au id.au asn.au
co.nz org.nz govt.nz ac.nz net.nz school.nz
com.br org.br gov.br net.br edu.br
com.mx org.mx gob.mx edu.mx net.mx
com.ar gob.ar org.ar net.ar edu.ar gov.ar
com.co org.co gov.co edu.co net.co
com.pe gob.pe org.pe edu.pe
com.ve com.uy com.ec com.bo com.py com.gt com.pa
co.jp or.jp ne.jp ac.jp go.jp
com.cn org.cn net.cn gov.cn edu.cn
co.in net.in org.in gov.in ac.in
co.za org.za gov.za ac.za net.za
com.tr org.tr gov.tr edu.tr
com.pl org.pl net.pl edu.pl gov.pl
com.pt org.pt gov.pt edu.pt
gouv.fr asso.fr com.fr
gov.it edu.it
co.kr or.kr go.kr ac.kr
com.hk org.hk gov.hk edu.hk
com.sg org.sg gov.sg edu.sg
com.my gov.my edu.my
co.id or.id go.id ac.id
co.th ac.th go.th
com.ph org.ph gov.ph edu.ph
com.vn gov.vn
co.il org.il gov.il ac.il
co.ae com.sa com.eg com.ng co.ke co.tz
com.ua org.ua
com.ru
""")


def registrable_domain(host: Any, extra_suffixes: Iterable[str] = ()) -> Optional[str]:
    """The registrable domain of a host (the public suffix plus one label): ``a.b.co.uk`` -> ``b.co.uk``,
    ``www.hacienda.gob.es`` -> ``hacienda.gob.es``, ``blog.example.com`` -> ``example.com``. IP literals and
    single-label names come back unchanged; ``None`` for an empty host. ``extra_suffixes`` adds suffixes of your own
    (``["internal.corp"]``)."""
    h = clean_host(host if "/" not in str(host or "") else host_of(host))
    if not h:
        return None
    try:
        ipaddress.ip_address(h)
        return h
    except ValueError:
        pass
    labels = h.split(".")
    if len(labels) <= 2:
        return h
    extra = {s.lower().strip(".") for s in extra_suffixes}
    for n in (3, 2):                    # longest suffix first
        if len(labels) > n and ".".join(labels[-n:]) in extra:
            return ".".join(labels[-(n + 1):])
    two = ".".join(labels[-2:])
    if two in PUBLIC_SUFFIXES or two in extra:
        return ".".join(labels[-3:])
    return two
