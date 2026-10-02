"""Web search: result-page parsers, rank fusion and an engine runner that uses the shared :class:`~.fetch.Fetcher`.

Pure helpers (no network): :func:`rrf_merge`, the freshness maps, :func:`parse_ddg_html`, :func:`parse_bing_html`,
:func:`parse_searxng_json`, :func:`parse_brave_json`, :func:`unwrap_ddg`, :func:`unwrap_bing`, :func:`is_blocked_page`.
A hit is a dict ``{url, title, snippet, engine, rank, published}``.

:class:`WebSearch` runs the engines key-less where it can: SearXNG (when a URL is configured), DuckDuckGo HTML, Bing
HTML, Brave (with a key), Google News RSS and Bing News RSS. Every engine may fail on its own; failures are reported per
engine and never raised, and nothing here solves a CAPTCHA (a blocked answer is reported as ``blocked``). Every request
goes through the Fetcher, so the SSRF policy, the per-host throttle and the block cooldown are shared with the rest of the
family: two apps searching DuckDuckGo no longer trip its burst limit independently.

This module replaces: Tantalus ``search.py`` (engines, parsers, fusion, freshness), the URL helpers it carried
(:mod:`.urls`, :mod:`.safety`) and Faustus' separate reciprocal-rank fusion.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit

from . import safety
from .feeds import parse_news_rss, to_iso_utc
from .htmltext import Node, parse_html
from .urls import host_of, url_key

__all__ = [
    "RRF_K", "DEFAULT_ENGINES", "NEWS_ENGINES", "SEARCH_MIN_INTERVAL_S", "rrf_merge", "ddg_freshness", "bing_freshness",
    "brave_freshness", "searxng_time_range", "unwrap_ddg", "unwrap_bing", "parse_ddg_html", "parse_bing_html",
    "parse_searxng_json", "parse_brave_json", "is_blocked_page", "WebSearch",
]

log = logging.getLogger("hoard_link.web.search")

RRF_K = 60
DEFAULT_ENGINES = ("searxng", "ddg", "bing", "brave", "gnews", "bingnews")
NEWS_ENGINES = ("gnews", "bingnews")
# Seconds between two calls to one engine. DuckDuckGo answers 202 (bot check) to bursts.
SEARCH_MIN_INTERVAL_S = {"ddg": 8.0, "bing": 3.0, "gnews": 3.0, "bingnews": 3.0, "brave": 1.0, "searxng": 1.0}


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", "" if text is None else str(text)).strip()


def _hit(url: str, title: str, snippet: str, engine: str, rank: int, published: Optional[str] = None) -> dict[str, Any]:
    return {"url": url, "title": title, "snippet": snippet, "engine": engine, "rank": rank, "published": published}


# ---- fusion -----------------------------------------------------------------------------------

def rrf_merge(rankings: Sequence[Sequence[Mapping[str, Any]]], k: int = RRF_K, weights: Any = None) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion over several rankings, de-duplicated by :func:`~.urls.url_key`.

    The score of a URL is the sum over rankings of ``weight / (k + rank)`` (the hit's own ``rank`` when it has one, else
    its position). ``weights`` is a list (one per ranking), a dict ``{engine: weight}`` or ``None`` (all 1). The best
    ranked copy supplies title, snippet and date; later copies only fill gaps; ``engine`` joins the engines that found
    it (``"ddg+bing"``); the result is sorted by score then first appearance, ``rank`` renumbered from 1 and ``score``
    added."""
    scores: dict[str, float] = {}
    best: dict[str, dict[str, Any]] = {}
    engines: dict[str, list[str]] = {}
    order: dict[str, int] = {}
    for i, hits in enumerate(rankings):
        for position, hit in enumerate(hits, start=1):
            key = url_key(hit.get("url", ""))
            if not key:
                continue
            if isinstance(weights, Mapping):
                w = float(weights.get(hit.get("engine", ""), 1.0))
            elif weights is not None and i < len(weights):
                w = float(weights[i])
            else:
                w = 1.0
            scores[key] = scores.get(key, 0.0) + w / (k + (hit.get("rank") or position))
            order.setdefault(key, len(order))
            current = best.get(key)
            if current is None:
                best[key] = {"url": hit.get("url", ""), "title": hit.get("title", ""), "snippet": hit.get("snippet", ""),
                             "engine": hit.get("engine", ""), "rank": hit.get("rank"), "published": hit.get("published")}
            else:
                current["snippet"] = current["snippet"] or hit.get("snippet", "")
                current["title"] = current["title"] or hit.get("title", "")
                current["published"] = current["published"] or hit.get("published")
            names = engines.setdefault(key, [])
            engine = hit.get("engine", "")
            if engine and engine not in names:
                names.append(engine)
    merged = []
    for position, key in enumerate(sorted(scores, key=lambda x: (-scores[x], order[x])), start=1):
        hit = best[key]
        hit["engine"] = "+".join(engines.get(key) or [hit["engine"]])
        hit["rank"] = position
        hit["score"] = round(scores[key], 6)
        merged.append(hit)
    return merged


# ---- freshness maps ---------------------------------------------------------------------------

def ddg_freshness(days: Optional[int]) -> str:
    if not days:
        return ""
    return "d" if days <= 1 else "w" if days <= 7 else "m" if days <= 31 else "y"


def bing_freshness(days: Optional[int]) -> str:
    if not days:
        return ""
    return 'ex1:"ez1"' if days <= 1 else 'ex1:"ez2"' if days <= 7 else 'ex1:"ez3"' if days <= 31 else ""


def brave_freshness(days: Optional[int]) -> str:
    if not days:
        return ""
    return "pd" if days <= 1 else "pw" if days <= 7 else "pm" if days <= 31 else "py"


def searxng_time_range(days: Optional[int]) -> str:
    if not days:
        return ""
    return "day" if days <= 1 else "week" if days <= 7 else "month" if days <= 31 else "year"


# ---- redirects and parsers --------------------------------------------------------------------

def unwrap_ddg(href: Any) -> str:
    """The real target of a DuckDuckGo result link (``//duckduckgo.com/l/?uddg=<url>``); ``""`` for an ad or internal
    link; other links come back unchanged."""
    href = ("" if href is None else str(href)).strip()
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if (parts.hostname or "").endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        for key, value in parse_qsl(parts.query):
            if key == "uddg" and value:
                return unquote(value)
        return ""
    return href


def unwrap_bing(href: Any) -> str:
    """The real target of a Bing result link (``bing.com/ck/a?...&u=a1<base64>``); ``""`` when it cannot be decoded."""
    href = ("" if href is None else str(href)).strip()
    parts = urlsplit(href)
    if (parts.hostname or "").endswith("bing.com") and parts.path.startswith("/ck/"):
        for key, value in parse_qsl(parts.query):
            if key == "u" and value.startswith("a1"):
                raw = value[2:]
                try:
                    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
                except ValueError:
                    return ""
        return ""
    return href


def _text(n: Optional[Node]) -> str:
    return _clean("".join(x.text for x in n.iter() if x.is_text)) if n is not None else ""


def _by_class(root: Node, tag: str, cls: str) -> list[Node]:
    return [n for n in root.iter() if n.tag == tag and cls in n.classes]


def _first_class(root: Node, cls: str, tag: Optional[str] = None) -> Optional[Node]:
    return next((n for n in root.iter() if cls in n.classes and (tag is None or n.tag == tag)), None)


def parse_ddg_html(html: Any) -> list[dict[str, Any]]:
    """Hits of a ``html.duckduckgo.com/html`` result page. Ads and internal links are dropped."""
    doc = parse_html(html)
    hits: list[dict[str, Any]] = []
    for block in _by_class(doc, "div", "result"):
        if "result--ad" in block.classes:
            continue
        link = _first_class(block, "result__a", "a")
        if link is None:
            continue
        url = unwrap_ddg(link.get("href"))
        if not url or host_of(url).endswith("duckduckgo.com"):
            continue
        hits.append(_hit(url, _text(link), _text(_first_class(block, "result__snippet")), "ddg", len(hits) + 1))
    return hits


def parse_bing_html(html: Any) -> list[dict[str, Any]]:
    """Hits of a ``bing.com/search`` result page."""
    doc = parse_html(html)
    hits: list[dict[str, Any]] = []
    for block in (n for n in doc.iter() if n.tag == "li" and "b_algo" in n.classes):
        h2 = next((n for n in block.iter() if n.tag == "h2"), None)
        link = next((n for n in h2.iter() if n.tag == "a"), None) if h2 is not None else None
        if link is None:
            continue
        url = unwrap_bing(link.get("href"))
        if not url or host_of(url).endswith("bing.com"):
            continue
        caption = _first_class(block, "b_caption")
        snippet_node = (next((n for n in caption.iter() if n.tag == "p"), None) if caption is not None else None) \
            or _first_class(block, "b_lineclamp2", "p") or caption
        hits.append(_hit(url, _text(link), _text(snippet_node), "bing", len(hits) + 1))
    return hits


def _json(data: Any) -> Any:
    if isinstance(data, (str, bytes)):
        import json
        try:
            return json.loads(data)
        except ValueError:
            return None
    return data


def parse_searxng_json(data: Any) -> list[dict[str, Any]]:
    """Hits of a SearXNG ``format=json`` answer (a dict or its text)."""
    d = _json(data)
    out: list[dict[str, Any]] = []
    for item in (d or {}).get("results") or [] if isinstance(d, dict) else []:
        if isinstance(item, dict) and item.get("url"):
            out.append(_hit(str(item["url"]), _clean(item.get("title", "")), _clean(item.get("content", "")), "searxng",
                            len(out) + 1, item.get("publishedDate") or None))
    return out


def parse_brave_json(data: Any) -> list[dict[str, Any]]:
    """Hits of a Brave Search API answer."""
    d = _json(data)
    out: list[dict[str, Any]] = []
    for item in ((d or {}).get("web") or {}).get("results") or [] if isinstance(d, dict) else []:
        if isinstance(item, dict) and item.get("url"):
            out.append(_hit(str(item["url"]), _clean(re.sub(r"<[^>]+>", "", str(item.get("title", "")))),
                            _clean(re.sub(r"<[^>]+>", "", str(item.get("description", "")))), "brave", len(out) + 1,
                            item.get("page_age") or None))
    return out


def is_blocked_page(html: Any, hits: Sequence[Any]) -> bool:
    """A results page without results that looks like a bot check (reported, never bypassed)."""
    if hits:
        return False
    low = ("" if html is None else str(html)).lower()
    return any(t in low for t in ("anomaly-modal", "captcha", "unusual traffic", "are you a human", "/sorry/", "challenge"))


# ---- the engine runner ------------------------------------------------------------------------

def _name_safe(url: str) -> bool:
    """A result URL a person may be sent to: http(s), no credentials, no local names or private literals (no DNS)."""
    return safety.check_url(url, safety.PUBLIC, lambda h, p: ["93.184.216.34"]) is None


class WebSearch:
    """``search(query, limit, freshness_days=..., engines=..., news=...) -> (hits, {engine: error})``.

    ``fetcher`` is a :class:`~.fetch.Fetcher` (or anything with ``get`` and ``get_json``). ``searxng_url`` and
    ``brave_key`` enable those engines; ``lang`` / ``region`` are the interface language and country the engines are
    asked for (``"es"`` / ``"ES"``, the family default). ``intervals`` overrides :data:`SEARCH_MIN_INTERVAL_S`.
    ``clock`` is only used for the news freshness filter."""

    def __init__(self, fetcher: Any, *, searxng_url: Optional[str] = None, brave_key: Optional[str] = None, lang: str = "es",
                 region: str = "ES", intervals: Optional[Mapping[str, float]] = None, clock: Callable[[], float] = time.time):
        self.fetcher = fetcher
        self.searxng_url = (searxng_url or "").strip().rstrip("/")
        self.brave_key = (brave_key or "").strip()
        self.lang = lang
        self.region = region.upper()
        self.intervals = {**SEARCH_MIN_INTERVAL_S, **(intervals or {})}
        self.clock = clock

    def available_engines(self, news: bool = False) -> list[str]:
        if news:
            return list(NEWS_ENGINES)
        engines = []
        if self.searxng_url:
            engines.append("searxng")
        engines += ["ddg", "bing"]
        if self.brave_key:
            engines.append("brave")
        return engines

    def search(self, query: str, limit: int = 10, *, freshness_days: Optional[int] = None, engines: Optional[Iterable[str]] = None,
               news: bool = False) -> tuple[list[dict[str, Any]], dict[str, str]]:
        query = _clean(query)
        if not query:
            return [], {"query": "empty query"}
        chosen = [e for e in (list(engines) if engines else self.available_engines(news)) if e in DEFAULT_ENGINES]
        errors: dict[str, str] = {}
        rankings: list[list[dict[str, Any]]] = []
        for engine in chosen:
            try:
                hits, error = getattr(self, "_" + engine)(query, int(limit), freshness_days)
            except Exception as exc:                                  # noqa: BLE001 - one engine must never take the others down
                log.info("search engine %s failed: %s", engine, exc)
                hits, error = [], f"{type(exc).__name__}: {exc}"[:200]
            if error:
                errors[engine] = error
            safe = [h for h in hits if _name_safe(h["url"])]
            if safe:
                rankings.append(safe)
        return rrf_merge(rankings)[: max(1, int(limit))], errors

    # ---- helpers
    @staticmethod
    def _why(fr: Any) -> str:
        if fr is None:
            return "no answer"
        reason = fr.block_reason or fr.error or (f"http {fr.status}" if fr.status else "no answer")
        return ("blocked: " if getattr(fr, "blocked", False) else "") + reason

    def _get(self, engine: str, url: str, params: dict[str, Any]) -> Any:
        return self.fetcher.get(url, tier="http", params=params, accept="html", respect_robots=False,
                                min_interval_s=self.intervals.get(engine, 3.0))

    def _page(self, fr: Any, parser: Callable[[str], list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], str]:
        if fr is None or not fr.ok:
            return [], self._why(fr)
        hits = parser(fr.text)
        if not hits and fr.status == 202:                               # DuckDuckGo: 202 + bot-check page to bursts
            return [], "blocked: bot check (http 202)"
        if not hits:
            return [], "blocked: bot check" if is_blocked_page(fr.text, hits) else "no results"
        return hits, ""

    # ---- engines
    def _news_rss(self, url: str, params: dict[str, Any], engine: str, limit: int, days: Optional[int]) -> tuple[list[dict[str, Any]], str]:
        fr = self._get(engine, url, params)
        if fr is None or not fr.ok:
            return [], self._why(fr)
        hits = parse_news_rss(fr.text, engine)
        if days:
            cutoff = self.clock() - days * 86400
            kept = []
            for h in hits:
                ts = _iso_ts(h.get("published"))
                if ts is None or ts >= cutoff:
                    kept.append(h)
            hits = kept
        return hits[:limit], "" if hits else "no results"

    def _gnews(self, query: str, limit: int, days: Optional[int]) -> tuple[list[dict[str, Any]], str]:
        q = query + (f" when:{days}d" if days else "")
        return self._news_rss("https://news.google.com/rss/search", {"q": q, "hl": self.lang, "gl": self.region,
                                                                    "ceid": f"{self.region}:{self.lang}"}, "gnews", limit, days)

    def _bingnews(self, query: str, limit: int, days: Optional[int]) -> tuple[list[dict[str, Any]], str]:
        return self._news_rss("https://www.bing.com/news/search", {"q": query, "format": "rss", "setlang": self.lang}, "bingnews", limit, days)

    def _ddg(self, query: str, limit: int, days: Optional[int]) -> tuple[list[dict[str, Any]], str]:
        params = {"q": query, "kl": f"{self.lang}-{self.lang}" if self.lang != "en" else "us-en"}
        if ddg_freshness(days):
            params["df"] = ddg_freshness(days)
        return self._page(self._get("ddg", "https://html.duckduckgo.com/html/", params), parse_ddg_html)

    def _bing(self, query: str, limit: int, days: Optional[int]) -> tuple[list[dict[str, Any]], str]:
        params = {"q": query, "setlang": self.lang, "cc": self.region}
        if bing_freshness(days):
            params["filters"] = bing_freshness(days)
        return self._page(self._get("bing", "https://www.bing.com/search", params), parse_bing_html)

    def _brave(self, query: str, limit: int, days: Optional[int]) -> tuple[list[dict[str, Any]], str]:
        if not self.brave_key:
            return [], "no API key"
        params: dict[str, Any] = {"q": query, "count": min(20, max(1, limit)), "country": self.region, "search_lang": self.lang}
        if brave_freshness(days):
            params["freshness"] = brave_freshness(days)
        fr, data = self.fetcher.get_json("https://api.search.brave.com/res/v1/web/search", tier="http", params=params,
                                         headers={"X-Subscription-Token": self.brave_key, "Accept": "application/json"},
                                         respect_robots=False, min_interval_s=self.intervals.get("brave", 1.0))
        if fr is None or not fr.ok or not isinstance(data, dict):
            return [], self._why(fr)
        hits = parse_brave_json(data)
        return hits, "" if hits else "no results"

    def _searxng(self, query: str, limit: int, days: Optional[int]) -> tuple[list[dict[str, Any]], str]:
        if not self.searxng_url:
            return [], "not configured"
        params: dict[str, Any] = {"q": query, "format": "json", "language": self.lang}
        if searxng_time_range(days):
            params["time_range"] = searxng_time_range(days)
        endpoint = self.searxng_url + "/search"
        # the user's own instance is an operator-configured endpoint: private addresses are allowed for it
        fr, data = self.fetcher.get_json(endpoint, tier="http", params=params, respect_robots=False,
                                         min_interval_s=self.intervals.get("searxng", 1.0), profile=safety.OPERATOR_LOCAL)
        if fr is None or not fr.ok or not isinstance(data, dict):
            return [], self._why(fr)
        hits = parse_searxng_json(data)
        return hits, "" if hits else "no results"


def _iso_ts(value: Any) -> Optional[float]:
    iso = to_iso_utc(value) if value else ""
    if not iso:
        return None
    import datetime as _dt
    return _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()
