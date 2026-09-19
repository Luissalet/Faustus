"""Search result ranking based on relevance, source quality, and recency.

On top of the original relevance/authority/recency scoring, this module adds
four explainable, deterministic guards (documented inline where each is
applied):

1. Every ranked result carries ``score`` and ``score_reasons`` so a caller
   can show *why* a result landed where it did.
2. Multi-engine agreement: when a result was returned by several search
   engines (SearXNG's own merge, or several providers merged upstream and
   deduplicated here by canonical URL), it gets a bounded reciprocal-rank
   fusion (RRF, k=60) boost over an equally-relevant single-engine result.
3. Anti-junk guards: a result that shares none of the query's content words
   is demoted below every result that shares at least one (never below all
   of them, if none share any); and, for very short (<=2 content word)
   queries, shop/store-looking domains are demoted unless the query itself
   carries purchase intent.
4. A freshness window: for a query ``src/freshness.py`` flags as
   time-sensitive, results whose parsed date is older than the window are
   demoted, with a conservative floor so demotion never empties the top of
   the list.
"""

import re
import logging
import unicodedata
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_AGE_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")


def _utcnow_naive() -> datetime:
    """Naive UTC 'now'. Matches the naive, UTC-style published dates parsed below,
    and is safe on Python 3.14 where ``datetime.utcnow()`` is removed (#1116)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_age(age_str: Optional[str]) -> Optional[datetime]:
    """Parse a result's ``age``/date field with the same formats ``recency_score``
    accepts. Returns ``None`` when it isn't a recognizable date (not an error —
    plenty of providers never send one)."""
    if not age_str:
        return None
    for fmt in _AGE_FORMATS:
        try:
            return datetime.strptime(age_str, fmt)
        except Exception:
            continue
    return None


def recency_score(age_str: Optional[str], now: Optional[datetime] = None) -> float:
    """Score how recent a result is: 1.0 for <=7 days old, 0.0 for >=30 days.

    The age is measured against UTC, not local time. The previous code used
    ``datetime.now()`` (local) against UTC-style published dates, so the age was
    skewed by the host's UTC offset; it was also a latent crash once neighbouring
    code moves to timezone-aware datetimes (#1116). ``now`` is injectable for tests.
    """
    dt = _parse_age(age_str)
    if not dt:
        return 0.0
    now = now or _utcnow_naive()
    days_old = (now - dt).days
    if days_old <= 7:
        return 1.0
    if days_old >= 30:
        return 0.0
    return (30 - days_old) / 23


_NEWS_HINTS = {"news", "nyheter", "headlines", "breaking", "latest", "today", "idag"}
_SPORTS_HINTS = {
    "sport", "sports", "soccer", "football", "hockey", "nba", "nfl", "mlb",
    "fifa", "world cup", "championship", "quarterfinal", "eliminates",
}
# Word-boundary match so "sport" does not fire inside "transport"/"passport"
# and a domain like "transport.gov" is not mistaken for a sports site.
_SPORTS_HINT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(h) for h in _SPORTS_HINTS) + r")\b"
)
_LOW_VALUE_NEWS_DOMAINS = {
    "facebook.com", "www.facebook.com", "sports.yahoo.com", "yahoo.com",
    "www.yahoo.com", "msn.com", "www.msn.com",
}
_TRUSTED_NEWS_DOMAINS = {
    "apnews.com", "www.apnews.com", "reuters.com", "www.reuters.com",
    "bbc.com", "www.bbc.com", "cbc.ca", "www.cbc.ca",
    "ctvnews.ca", "www.ctvnews.ca", "globalnews.ca", "www.globalnews.ca",
    "theguardian.com",
    "www.theguardian.com", "euronews.com", "www.euronews.com",
    "dw.com", "www.dw.com", "government.se", "www.government.se",
}


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _has_word(text: str, term: str) -> bool:
    """True if ``term`` appears in ``text`` as a whole word.

    Query terms are matched on word boundaries so a short term doesn't match
    inside an unrelated word: "us" must not match "business"/"music", "port"
    must not match "transport"/"support". This mirrors the tokenization used to
    build ``query_terms`` (``\\b\\w+\\b``). #1473 converted the title and sports
    checks to word boundaries; the snippet and subject-term checks below use
    the same helper so the whole file stays consistent.
    """
    return re.search(rf"\b{re.escape(term)}\b", text) is not None


# ---------------------------------------------------------------------------
# Guard 1: explainability plumbing is inline in rank_search_results below.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Guard 2: multi-engine agreement (reciprocal rank fusion).
# ---------------------------------------------------------------------------

_RRF_K = 60
# Scales the raw RRF increment (a fraction like 0.03) up into a score range
# comparable to the other weighted terms below, capped so agreement can never
# swamp genuine relevance/authority.
_RRF_SCALE = 10.0
_RRF_CAP = 0.6

try:
    # Reused, not reimplemented: this is the same canonical-URL normalizer
    # `src/research_citations.py` uses to decide whether two fetches are one
    # source (case-folds scheme/host, drops default port/fragment/trailing
    # slash/tracking params). No network, no LLM, pure stdlib — safe to share.
    from src.research_citations import canonical_url as _canonical_url
except Exception:  # pragma: no cover - defensive, keeps ranking usable standalone
    def _canonical_url(url):  # type: ignore
        return (str(url or "")).strip().lower()

try:
    from src.source_types import classify_source as _classify_source
except Exception:  # pragma: no cover - defensive, keeps ranking usable standalone
    def _classify_source(url, title=""):  # type: ignore
        return {"source_type": "unknown", "is_official": False, "reason": "unavailable"}


# ---------------------------------------------------------------------------
# Guard 5: source-type nudge — small, bounded and explained via score_reasons.
# ---------------------------------------------------------------------------

# Kept deliberately small relative to the other terms above (title match is
# worth up to 2.0, authority up to 1.5) so this never overrides genuine
# relevance/authority signals -- it only nudges close calls between
# similarly-relevant results.
_SOURCE_TYPE_UP = 0.3
_SOURCE_TYPE_DOWN = 0.3
_SOURCE_TYPES_BOOSTED = {"official", "docs", "academic", "reference"}


def _result_engines(result: dict) -> List[str]:
    """Every engine/provider name attached to a single raw result dict.

    SearXNG's own merge puts the list under ``engines`` (raw JSON) or
    ``_engines``/``_engine`` (as normalized by ``providers.searxng_search_api``).
    A plain single-provider result (Brave, Tavily, ...) carries none of these,
    which is correctly read as "one engine, unknown name".
    """
    names: List[str] = []
    for key in ("_engines", "engines"):
        val = result.get(key)
        if isinstance(val, (list, tuple)):
            names.extend(str(v) for v in val if v)
    single = result.get("_engine") or result.get("engine") or result.get("provider")
    if single:
        names.append(str(single))
    return names or ["engine-unknown"]


def _dedupe_and_fuse(results: List[dict]) -> List[Tuple[dict, List[str], int]]:
    """Group raw results by canonical URL and compute each survivor's combined
    engine list plus its rank (0-based position of first occurrence).

    This is the "dedupe by canonical URL before fusing" step: when the same
    page comes back more than once (SearXNG already merged its own engines
    per result, but nothing stops two providers merged upstream from handing
    back the same page twice), the duplicates collapse into one result whose
    engine list is the union of all the copies' engines, and its rank is
    wherever it first appeared.
    """
    order: List[str] = []
    by_key: Dict[str, dict] = {}
    engines_by_key: Dict[str, List[str]] = {}
    rank_by_key: Dict[str, int] = {}

    for idx, result in enumerate(results):
        url = result.get("url", "")
        key = _canonical_url(url) or f"__no_url_{idx}"
        engines = _result_engines(result)
        if key not in by_key:
            by_key[key] = result
            engines_by_key[key] = list(dict.fromkeys(engines))
            rank_by_key[key] = idx
            order.append(key)
        else:
            # Same page seen again (either a second engine inside one SearXNG
            # response somehow slipped through unmerged, or a second
            # provider's copy): fold its engines in, keep the first-seen
            # result dict/rank so ordering stays deterministic.
            existing = engines_by_key[key]
            for eng in engines:
                if eng not in existing:
                    existing.append(eng)

    return [(by_key[k], engines_by_key[k], rank_by_key[k]) for k in order]


def _rrf_boost(engine_count: int, rank_index: int) -> float:
    """Bounded reciprocal-rank-fusion boost for a result ``engine_count``
    engines agreed on, at merged rank ``rank_index`` (0-based).

    True RRF sums ``1/(k+rank)`` per engine using that engine's *own* rank of
    the result; the per-engine rank isn't available once SearXNG (or an
    upstream merge) has already fused the list into one ordering, so every
    agreeing engine is credited at the same merged rank plus a small per-extra-
    engine offset — this keeps a result several engines picked out ranked
    above an equally-relevant single-engine result without needing per-engine
    position data that doesn't exist here.
    """
    if engine_count <= 1:
        return 0.0
    total = sum(1.0 / (_RRF_K + rank_index + i) for i in range(engine_count))
    single = 1.0 / (_RRF_K + rank_index)
    raw = total - single
    return min(_RRF_CAP, raw * _RRF_SCALE)


# ---------------------------------------------------------------------------
# Guard 3: anti-junk — zero query-term overlap, and brand/shop collisions.
# ---------------------------------------------------------------------------

_STOPWORDS_EN = frozenset(
    "a an the and or of to in is are was were that this these those for with "
    "as it its on at from by be been what how why when which who not no do "
    "does did have has had can could should would will about between than "
    "more most best".split()
)
_STOPWORDS_ES = frozenset(
    "el la los las un una unos unas y o de del que en por para con como es "
    "son era sobre segun que cual cuales cuanto cuanta cuantos donde como "
    "porque al se no ni hay mas entre desde tienen tiene sus su este esta "
    "estos estas".split()
)


def _fold_accents(text: str) -> str:
    """Accent-fold to plain ASCII-ish lowercase (``precio``/``PRECIO``/``prècio``
    all compare equal) without pulling in a locale library."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()


def _content_terms(query_terms: List[str]) -> List[str]:
    """Query terms with ES/EN stopwords removed, accent-folded.

    Only used for the anti-junk guards below (overlap + brand collision) —
    the original relevance scoring below still uses the raw, un-stripped
    ``query_terms`` so existing behaviour there is unchanged.
    """
    terms = []
    for term in query_terms:
        folded = _fold_accents(term)
        if not folded or folded in _STOPWORDS_EN or folded in _STOPWORDS_ES:
            continue
        terms.append(folded)
    return terms


# A fixed penalty rather than an absolute rank floor: this reliably sinks a
# genuinely off-topic result below on-topic ones while still letting a very
# strong authority/relevance signal on other axes (e.g. a trusted domain the
# news-quality adjustment already likes) outweigh it in close calls, instead
# of a hard rule silently overriding every other factor in this file.
_ZERO_OVERLAP_DEMOTION = 2.0


def _has_overlap(text: str, content_terms: List[str]) -> bool:
    if not content_terms:
        return True  # nothing to check against -> don't penalize anyone
    folded = _fold_accents(text)
    return any(_has_word(folded, term) for term in content_terms)


_SHOP_TLDS = (".shop", ".store", ".buy")
_SHOP_TOKENS = ("shop", "store", "tienda", "comprar", "buy", "cart")
_PURCHASE_INTENT_TERMS = frozenset(
    "buy price precio comprar tienda store shop cart purchase cheap barato "
    "oferta comprando".split()
)


def _looks_shop_like(url: str) -> bool:
    netloc = _domain(url)
    if not netloc:
        return False
    if netloc.endswith(_SHOP_TLDS):
        return True
    try:
        path = urlparse(url).path.lower()
    except Exception:
        path = ""
    haystack = f"{netloc} {path}"
    return any(_has_word(haystack.replace(".", " ").replace("/", " "), tok) for tok in _SHOP_TOKENS)


def _has_purchase_intent(content_terms: List[str]) -> bool:
    return any(term in _PURCHASE_INTENT_TERMS for term in content_terms)


# ---------------------------------------------------------------------------
# Guard 4: freshness window (time-sensitive queries only).
# ---------------------------------------------------------------------------

# Maps a src/freshness.py reason label to how strict its staleness window is.
# "day" queries (live scores, breaking news, weather, availability) go stale
# fastest; "week" queries (a release, an office holder) tolerate more age;
# anything else falls back to the default 30-day window.
_FRESHNESS_REASON_WINDOW = {
    "sports_result": "day",
    "news_events": "day",
    "prices_markets": "day",
    "weather": "day",
    "schedules": "day",
    "availability": "day",
    "releases_versions": "week",
    "office_status": "week",
}
_WINDOW_DAYS = {"day": 2, "week": 10, "default": 30}
_STALE_DEMOTION = 4.0


def _freshness_window_days(query: str) -> Optional[int]:
    """Days after which a result is "stale" for this query, or ``None`` if the
    query isn't time-sensitive at all (``src/freshness.py`` found no reason)."""
    try:
        from src.freshness import freshness_reasons
    except Exception:  # pragma: no cover - defensive, ranking must not hard-depend on it
        return None
    reasons = freshness_reasons(query)
    if not reasons:
        return None
    categories = {_FRESHNESS_REASON_WINDOW.get(r, "default") for r in reasons}
    if "day" in categories:
        return _WINDOW_DAYS["day"]
    if "week" in categories:
        return _WINDOW_DAYS["week"]
    return _WINDOW_DAYS["default"]


def rank_search_results(query: str, results: List[dict]) -> List[dict]:
    """Rank search results by title relevance, snippet quality, domain authority,
    and recency, then apply the multi-engine, anti-junk and freshness guards.

    Every returned dict keeps its original keys plus ``score`` (float) and
    ``score_reasons`` (list[str], short human-readable factors).
    """
    query_terms = [t.lower() for t in re.findall(r"\b\w+\b", query)]
    query_lc = query.lower()
    is_news_query = any(term in _NEWS_HINTS for term in query_terms)
    is_sports_query = bool(_SPORTS_HINT_RE.search(query_lc))
    content_terms = _content_terms(query_terms)
    purchase_intent = _has_purchase_intent(content_terms)
    freshness_window = _freshness_window_days(query)

    def title_score(title: str) -> float:
        if not title:
            return 0.0
        title_lc = title.lower()
        matches = sum(1 for term in query_terms if _has_word(title_lc, term))
        return matches / len(query_terms) if query_terms else 0.0

    def title_reason(title: str) -> Optional[str]:
        if not query_terms or not title:
            return None
        title_lc = title.lower()
        matches = sum(1 for term in query_terms if _has_word(title_lc, term))
        return f"title match {matches}/{len(query_terms)}"

    def snippet_score(snippet: str) -> float:
        if not snippet:
            return 0.0
        length_factor = min(len(snippet), 200) / 200
        term_hits = sum(1 for term in query_terms if _has_word(snippet.lower(), term))
        term_factor = term_hits / len(query_terms) if query_terms else 0.0
        return (length_factor + term_factor) / 2

    def domain_score(url: str) -> float:
        netloc = _domain(url)
        if not netloc:
            return 0.0
        if netloc in _TRUSTED_NEWS_DOMAINS:
            return 1.0
        if netloc.endswith(".edu") or netloc.endswith(".gov"):
            return 1.0
        if netloc.endswith(".org"):
            return 0.7
        return 0.4

    def news_quality_adjustment(title: str, snippet: str, url: str) -> float:
        if not is_news_query:
            return 0.0
        text = f"{title} {snippet}".lower()
        netloc = _domain(url)
        adjustment = 0.0
        if netloc in _TRUSTED_NEWS_DOMAINS:
            adjustment += 1.2
        if any(term in text for term in ("latest news", "breaking news", "daily coverage", "news from")):
            adjustment += 0.4
        if netloc in _LOW_VALUE_NEWS_DOMAINS:
            adjustment -= 0.8
        if not is_sports_query and (_SPORTS_HINT_RE.search(text) or _SPORTS_HINT_RE.search(netloc)):
            adjustment -= 1.5
        # A country/news query should not rank a page whose title/snippet barely
        # mentions the country above actual news pages for that country.
        subject_terms = [t for t in query_terms if t not in _NEWS_HINTS]
        if subject_terms and not any(_has_word(text, t) or _has_word(netloc, t) for t in subject_terms):
            adjustment -= 1.0
        return adjustment

    # --- dedupe by canonical URL + base score + multi-engine boost ---------
    fused = _dedupe_and_fuse(results)

    scored = []  # (base_score, reasons, result, engine_count, input_rank)
    for result, engines, rank_index in fused:
        title = result.get("title", "")
        snippet = result.get("snippet", "")
        url = result.get("url", "")
        age = result.get("age", None)

        reasons: List[str] = []
        t_reason = title_reason(title)
        if t_reason:
            reasons.append(t_reason)

        dscore = domain_score(url)
        if dscore >= 1.0:
            reasons.append("authority +1.0 (trusted/edu/gov)")
        elif dscore >= 0.7:
            reasons.append("authority +0.7 (.org)")

        rscore = recency_score(age)
        if rscore >= 1.0:
            reasons.append("recent (<=7 d)")
        elif rscore > 0.0:
            reasons.append("recency partial")

        engine_count = len(engines)
        rrf = _rrf_boost(engine_count, rank_index)
        if engine_count > 1:
            reasons.append(f"multi-engine x{engine_count}")

        source_info = _classify_source(url, title)
        source_type = source_info.get("source_type", "unknown")
        is_official = bool(source_info.get("is_official"))

        source_type_adjustment = 0.0
        if source_type in _SOURCE_TYPES_BOOSTED:
            source_type_adjustment += _SOURCE_TYPE_UP
            reasons.append(f"source type: {source_type} (+{_SOURCE_TYPE_UP:.1f})")
        elif source_type == "shop" and not purchase_intent:
            source_type_adjustment -= _SOURCE_TYPE_DOWN
            reasons.append(f"source type: shop, no purchase intent (-{_SOURCE_TYPE_DOWN:.1f})")

        base = (
            2.0 * title_score(title)
            + 1.0 * snippet_score(snippet)
            + 1.5 * dscore
            + 1.0 * rscore
            + news_quality_adjustment(title, snippet, url)
            + rrf
            + source_type_adjustment
        )
        scored.append({
            "score": base,
            "reasons": reasons,
            "result": result,
            "url": url,
            "title": title,
            "snippet": snippet,
            "age": age,
            "source_type": source_type,
            "is_official": is_official,
        })

    # --- anti-junk guard (a): zero content-term overlap demotion -----------
    if content_terms:
        overlaps = [
            _has_overlap(f"{item['title']} {item['snippet']}", content_terms)
            for item in scored
        ]
        any_overlap = any(overlaps)
        if any_overlap:
            for item, has_overlap in zip(scored, overlaps):
                if not has_overlap:
                    item["score"] -= _ZERO_OVERLAP_DEMOTION
                    item["reasons"].append("no term overlap")

    # --- anti-junk guard (b): brand/shop collision for very short queries --
    if len(content_terms) <= 2 and content_terms and not purchase_intent:
        for item in scored:
            if _looks_shop_like(item["url"]):
                item["score"] -= 3.0
                item["reasons"].append("brand-collision (shop-like domain)")
    elif purchase_intent:
        for item in scored:
            if _looks_shop_like(item["url"]):
                item["reasons"].append("purchase intent (not penalized)")

    # --- freshness window ---------------------------------------------------
    if freshness_window is not None:
        now = _utcnow_naive()
        stale_flags = []
        for item in scored:
            dt = _parse_age(item["age"])
            if dt is None:
                stale_flags.append((False, None))
                continue
            age_days = (now - dt).days
            stale_flags.append((age_days > freshness_window, age_days))
        non_stale = sum(1 for is_stale, _ in stale_flags if not is_stale)
        # Conservative fallback: only demote stale results when at least 3
        # non-stale ones remain to fill the top of the list — otherwise
        # demoting everything just empties the top for no benefit, and
        # nothing is removed either way.
        if non_stale >= 3:
            for item, (is_stale, age_days) in zip(scored, stale_flags):
                if is_stale:
                    item["score"] -= _STALE_DEMOTION
                    item["reasons"].append(f"stale ({age_days} d)")

    # --- final sort (stable: original/fused order breaks ties) -------------
    scored.sort(key=lambda item: item["score"], reverse=True)

    ranked = []
    for item in scored:
        out = dict(item["result"])
        out["score"] = round(item["score"], 4)
        out["score_reasons"] = item["reasons"]
        out["source_type"] = item["source_type"]
        out["is_official"] = item["is_official"]
        ranked.append(out)
    return ranked
