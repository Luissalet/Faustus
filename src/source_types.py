"""Deterministic, table-driven classification of search results and fetched
page content.

Two pure functions, no network, no LLM:

- ``classify_source(url, title="")`` -> a conservative ``source_type`` guess
  (official/docs/code/academic/news/reference/forum/social/shop/blog/unknown)
  plus ``is_official`` for the URL's host and path.
- ``extraction_quality(text, html=None)`` -> a 0..1 quality score plus flags
  (thin/boilerplate_heavy/link_farm/paywall_or_login/js_required/error_page)
  for text extracted from a fetched page, so a caller can tell a real article
  apart from a login wall, a cookie banner or a 403 page.

Both are intentionally conservative: when a URL/text doesn't clearly match a
pattern, they fall back to "unknown" / no flags rather than guessing.
"""

import re
from typing import Dict, List, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# classify_source
# ---------------------------------------------------------------------------

# Government / official-body TLDs. These are the strongest "official" signal
# available from the URL alone.
_OFFICIAL_TLDS = (
    ".gov", ".gob", ".gouv", ".mil", ".int",
)
# Country-specific government second-level domains that don't end in a bare
# TLD above (e.g. "gc.ca", "gov.uk" is already covered by ".gov").
_OFFICIAL_SUFFIXES = (
    ".gc.ca", ".govt.nz", ".gob.mx", ".gob.ar", ".gob.es", ".gov.uk",
    ".gouv.fr", ".gouv.qc.ca",
)
# Official bodies on ordinary TLDs, matched on the registrable domain so
# subdomains count too (sede.agenciatributaria.gob.es is already covered by
# ".gob.es"; these are the ones a suffix rule can't see).
_OFFICIAL_DOMAINS = (
    "europa.eu", "boe.es", "ine.es", "bde.es", "cnmv.es", "who.int",
    "un.org", "oecd.org", "imf.org", "worldbank.org", "ecb.europa.eu",
)

_ACADEMIC_TLD_SUFFIXES = (".edu",)
# ``.ac.<cc>`` academic domains (ac.uk, ac.jp, ac.in, ac.nz, ac.za, ...).
_ACADEMIC_AC_RE = re.compile(r"\.ac\.[a-z]{2,3}$")
_ACADEMIC_HOSTS = {
    "arxiv.org", "doi.org", "dx.doi.org", "pubmed.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "scholar.google.com",
    "semanticscholar.org", "www.semanticscholar.org", "jstor.org",
    "www.jstor.org", "researchgate.net", "www.researchgate.net",
    "ssrn.com", "www.ssrn.com", "biorxiv.org", "www.biorxiv.org",
    "plos.org", "www.plos.org", "nature.com", "www.nature.com",
    "sciencedirect.com", "www.sciencedirect.com",
}

_DOCS_HOST_PREFIXES = ("docs.", "developer.", "developers.", "learn.", "api.")
_DOCS_DOMAINS = ("readthedocs.io", "readthedocs.org", "gitbook.io", "mintlify.app")

_CODE_HOSTS = {
    "github.com", "www.github.com", "raw.githubusercontent.com",
    "gist.github.com", "gitlab.com", "www.gitlab.com", "bitbucket.org",
    "sourceforge.net", "www.sourceforge.net", "pypi.org", "www.pypi.org",
    "npmjs.com", "www.npmjs.com", "crates.io", "rubygems.org",
    "hub.docker.com", "codeberg.org", "sr.ht", "git.sr.ht",
    "stackblitz.com", "codesandbox.io",
}

_REFERENCE_HOSTS_SUFFIXES = (
    "wikipedia.org", "wiktionary.org", "wikidata.org", "wikibooks.org",
    "wikimedia.org",
)
_REFERENCE_HOSTS = {
    "britannica.com", "www.britannica.com", "dictionary.com",
    "www.dictionary.com", "merriam-webster.com", "www.merriam-webster.com",
}

_FORUM_HOSTS_SUFFIXES = ("stackexchange.com",)
_FORUM_HOSTS = {
    "stackoverflow.com", "www.stackoverflow.com", "superuser.com",
    "serverfault.com", "askubuntu.com", "reddit.com", "www.reddit.com",
    "old.reddit.com", "quora.com", "www.quora.com",
    "news.ycombinator.com", "discourse.org",
}
_FORUM_HOST_PREFIXES = ("forum.", "forums.", "community.")

_SOCIAL_HOSTS = {
    "twitter.com", "x.com", "www.twitter.com", "www.x.com",
    "facebook.com", "www.facebook.com", "instagram.com", "www.instagram.com",
    "linkedin.com", "www.linkedin.com", "tiktok.com", "www.tiktok.com",
    "threads.net", "www.threads.net", "mastodon.social", "pinterest.com",
    "www.pinterest.com", "snapchat.com", "www.snapchat.com",
}

_SHOP_HOSTS_SUFFIXES = (
    "amazon.com", "amazon.co.uk", "amazon.de", "amazon.es", "amazon.fr",
    "ebay.com", "etsy.com", "aliexpress.com", "walmart.com", "target.com",
    "shopify.com", "mercadolibre.com",
)
_SHOP_PATH_TOKENS = ("/product/", "/products/", "/dp/", "/cart", "/checkout", "/shop/", "/tienda/")
_SHOP_HOST_TOKENS = ("shop.", "store.", "tienda.")

_BLOG_HOSTS_SUFFIXES = (
    "medium.com", "substack.com", "blogspot.com", "wordpress.com", "ghost.io",
)
_BLOG_PATH_RE = re.compile(r"(^|/)blog(/|$)")
_BLOG_HOST_PREFIXES = ("blog.",)

_NEWS_HOSTS = {
    "apnews.com", "www.apnews.com", "reuters.com", "www.reuters.com",
    "bbc.com", "www.bbc.com", "bbc.co.uk", "www.bbc.co.uk",
    "cnn.com", "www.cnn.com", "nytimes.com", "www.nytimes.com",
    "theguardian.com", "www.theguardian.com", "washingtonpost.com",
    "www.washingtonpost.com", "npr.org", "www.npr.org",
    "cbc.ca", "www.cbc.ca", "ctvnews.ca", "www.ctvnews.ca",
    "globalnews.ca", "www.globalnews.ca", "euronews.com", "www.euronews.com",
    "dw.com", "www.dw.com", "aljazeera.com", "www.aljazeera.com",
    "bloomberg.com", "www.bloomberg.com", "forbes.com", "www.forbes.com",
    "elpais.com", "www.elpais.com", "elmundo.es", "www.elmundo.es",
}
# News outlets matched on the registrable domain (any subdomain).
_NEWS_DOMAINS = (
    "elpais.com", "elmundo.es", "europapress.es", "efe.com", "rtve.es",
    "lavanguardia.com", "elconfidencial.com", "abc.es", "20minutos.es",
    "eldiario.es", "expansion.com", "elperiodico.com", "larazon.es",
    "reuters.com", "apnews.com", "bbc.co.uk", "bbc.com", "lemonde.fr",
)
_NEWS_HOST_PREFIXES = ("news.",)


def _host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().split("@")[-1].split(":")[0]
    except Exception:
        return ""


def _path(url: str) -> str:
    try:
        return (urlparse(url).path or "").lower()
    except Exception:
        return ""


def _suffix_match(host: str, suffixes) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _on_domain(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def classify_source(url: str, title: str = "") -> Dict[str, object]:
    """Classify ``url`` (and optionally its ``title``) into a coarse source
    type using conservative, table-driven URL/host rules only.

    Returns ``{"source_type": str, "is_official": bool, "reason": str}``.
    Falls back to ``{"source_type": "unknown", "is_official": False, ...}``
    when nothing matches -- this function never raises on a malformed URL.
    """
    url = url or ""
    host = _host(url)
    path = _path(url)

    if not host:
        return {"source_type": "unknown", "is_official": False, "reason": "no host"}

    # 0. Specific known academic hosts that happen to sit on a government TLD
    # (e.g. pubmed.ncbi.nlm.nih.gov is .gov but is a literature index, not an
    # official government statement) -- exact-host override, checked first.
    if host in _ACADEMIC_HOSTS:
        return {"source_type": "academic", "is_official": False, "reason": "known academic host"}

    # 1. Official (government/intergovernmental) -- strongest, checked first.
    if (host.endswith(_OFFICIAL_TLDS) or any(host.endswith(s) for s in _OFFICIAL_SUFFIXES)
            or _on_domain(host, _OFFICIAL_DOMAINS)):
        return {"source_type": "official", "is_official": True, "reason": "government TLD/suffix"}

    # 2. Docs -- official product/project documentation subdomains.
    if any(host.startswith(p) for p in _DOCS_HOST_PREFIXES) or _on_domain(host, _DOCS_DOMAINS):
        return {"source_type": "docs", "is_official": True, "reason": "docs-like host prefix"}

    # 3. Code hosts.
    if host in _CODE_HOSTS:
        return {"source_type": "code", "is_official": False, "reason": "known code host"}

    # 4. Academic.
    if (
        host.endswith(_ACADEMIC_TLD_SUFFIXES)
        or _ACADEMIC_AC_RE.search(host)
        or host in _ACADEMIC_HOSTS
    ):
        return {"source_type": "academic", "is_official": False, "reason": "academic host/TLD"}

    # 5. Reference (encyclopedic).
    if _suffix_match(host, _REFERENCE_HOSTS_SUFFIXES) or host in _REFERENCE_HOSTS:
        return {"source_type": "reference", "is_official": False, "reason": "reference host"}

    # 6. Forum / Q&A / discussion.
    if (
        _suffix_match(host, _FORUM_HOSTS_SUFFIXES)
        or host in _FORUM_HOSTS
        or any(host.startswith(p) for p in _FORUM_HOST_PREFIXES)
    ):
        return {"source_type": "forum", "is_official": False, "reason": "forum/Q&A host"}

    # 7. Social networks.
    if host in _SOCIAL_HOSTS:
        return {"source_type": "social", "is_official": False, "reason": "social network host"}

    # 8. News outlets (checked before generic blog/shop path rules).
    if (host in _NEWS_HOSTS or _on_domain(host, _NEWS_DOMAINS)
            or any(host.startswith(p) for p in _NEWS_HOST_PREFIXES)):
        return {"source_type": "news", "is_official": False, "reason": "known news host"}

    # 9. Shop / e-commerce.
    if _suffix_match(host, _SHOP_HOSTS_SUFFIXES) or any(host.startswith(t) for t in _SHOP_HOST_TOKENS):
        return {"source_type": "shop", "is_official": False, "reason": "known shop host"}
    if any(tok in path for tok in _SHOP_PATH_TOKENS):
        return {"source_type": "shop", "is_official": False, "reason": "shop-like path"}

    # 10. Blog.
    if _suffix_match(host, _BLOG_HOSTS_SUFFIXES) or any(host.startswith(p) for p in _BLOG_HOST_PREFIXES):
        return {"source_type": "blog", "is_official": False, "reason": "blog platform/host"}
    if _BLOG_PATH_RE.search(path):
        return {"source_type": "blog", "is_official": False, "reason": "blog-like path"}

    return {"source_type": "unknown", "is_official": False, "reason": "no rule matched"}


# ---------------------------------------------------------------------------
# extraction_quality
# ---------------------------------------------------------------------------

_THIN_CHAR_THRESHOLD = 250
_THIN_WORD_THRESHOLD = 40

_PAYWALL_LOGIN_PHRASES = (
    "subscribe to continue", "subscribe to read", "sign in to continue",
    "log in to continue", "please log in", "please sign in",
    "create a free account", "this content is for subscribers",
    "register to continue", "become a member to read",
    "accept cookies", "we use cookies", "cookie consent",
    "manage your cookie preferences", "enable cookies to continue",
    "this site uses cookies",
)
_JS_REQUIRED_PHRASES = (
    "enable javascript", "please enable javascript", "javascript is disabled",
    "you need to enable javascript", "javascript is required",
    "turn on javascript",
)
_ERROR_PAGE_PHRASES = (
    "403 forbidden", "access denied", "404 not found", "page not found",
    "this page isn't working", "internal server error",
    "service unavailable", "you don't have permission to access",
    "the page you requested could not be found",
)

_QUALITY_PENALTIES = {
    "thin": 0.5,
    "boilerplate_heavy": 0.2,
    "link_farm": 0.3,
    "paywall_or_login": 0.4,
    "js_required": 0.5,
    "error_page": 0.9,
}

_LINE_SPLIT_RE = re.compile(r"[\n\r]+|\s*\|\s*|\s*·\s*|\s*•\s*")


def _detect_phrases(text_lc: str, phrases) -> bool:
    return any(p in text_lc for p in phrases)


def _line_stats(text: str):
    """Split ``text`` into pseudo-"lines" (newlines and common menu
    separators) and report how nav/menu-like the result looks."""
    lines = [l.strip() for l in _LINE_SPLIT_RE.split(text) if l.strip()]
    if len(lines) < 6:
        return False, False
    short = [l for l in lines if len(l.split()) <= 3]
    short_ratio = len(short) / len(lines)
    boilerplate_heavy = len(lines) >= 8 and short_ratio > 0.6
    link_farm = len(lines) >= 15 and short_ratio > 0.75
    return boilerplate_heavy, link_farm


def extraction_quality(text: Optional[str], html: Optional[str] = None) -> Dict[str, object]:
    """Score extracted page ``text`` 0..1 with human-readable ``flags``.

    ``html`` is accepted for future link-density refinement but is optional;
    the current heuristic is text-only so this stays a pure, fast function
    usable on every fetch without re-parsing markup.
    """
    text = text or ""
    stripped = text.strip()
    text_lc = stripped.lower()

    flags: List[str] = []

    word_count = len(stripped.split())
    if len(stripped) < _THIN_CHAR_THRESHOLD or word_count < _THIN_WORD_THRESHOLD:
        flags.append("thin")

    boilerplate_heavy, link_farm = _line_stats(stripped)
    if boilerplate_heavy:
        flags.append("boilerplate_heavy")
    if link_farm:
        flags.append("link_farm")

    if _detect_phrases(text_lc, _PAYWALL_LOGIN_PHRASES):
        flags.append("paywall_or_login")
    if _detect_phrases(text_lc, _JS_REQUIRED_PHRASES):
        flags.append("js_required")
    if _detect_phrases(text_lc, _ERROR_PAGE_PHRASES):
        flags.append("error_page")

    score = 1.0
    for flag in flags:
        score -= _QUALITY_PENALTIES.get(flag, 0.0)
    score = max(0.0, min(1.0, score))

    return {"quality": round(score, 4), "flags": flags}


_QUALITY_NOTES = {
    "error_page": "This page returned an error/blocked page, not the article -- treat the content as unavailable, not as the source's actual text.",
    "paywall_or_login": "This page shows a paywall/login/cookie wall -- the extracted text is likely incomplete, not the full article.",
    "js_required": "This page requires JavaScript to render -- the extracted text may be missing most of the real content.",
}


def quality_note(flags: List[str]) -> str:
    """One-line, human-readable note for the model when ``flags`` (from
    :func:`extraction_quality`) contains a flag that means the extracted text
    is likely incomplete or wrong. Returns ``""`` when nothing applies."""
    for key in ("error_page", "paywall_or_login", "js_required"):
        if key in flags:
            return _QUALITY_NOTES[key]
    return ""
