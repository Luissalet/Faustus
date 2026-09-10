"""Search diversity/coverage measurement (WEB-01).

A result count is not diversity: SearXNG can answer "10 results" while all
ten come from one engine, or a follow-up query can come back with the same
ten URLs re-ranked -- the failure `tests/test_search_engine_health.py`
documents (09-09-2026, ``bing`` handing back the same pages for every
phrasing of a whiplash query). ``providers._record_engine_health`` already
tracks *which engines answered*; this module adds the other two readings the
spec asks for -- unique domains and overlap between consecutive queries --
as pure functions so ``GET /api/search/health`` and the deep-research loop
can both consult them without a second store (rule 4: reuse the existing
``ENGINE_HEALTH`` authority, don't duplicate it).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlparse

# Above this fraction, two consecutive queries are read as covering the same
# ground rather than different facets of the topic (spec: "dos consultas
# devuelven >70% de URLs iguales").
HIGH_OVERLAP_THRESHOLD = 0.7


def _domain(url: str) -> str:
    try:
        return (urlparse(str(url)).netloc or "").lower()
    except Exception:
        return ""


def unique_domains(urls: Iterable[str]) -> List[str]:
    """Sorted distinct domains among ``urls``; blanks and unparsable URLs drop out."""
    seen = {d for d in (_domain(u) for u in urls) if d}
    return sorted(seen)


def overlap_ratio(current_urls: Iterable[str], previous_urls: Iterable[str]) -> float:
    """Fraction of ``current_urls`` also present in ``previous_urls`` (0.0-1.0).

    An empty current batch has nothing to overlap (0.0), not a ZeroDivisionError
    and not "100% identical" -- an empty result set is not the same query
    repeating itself.
    """
    current = [str(u) for u in current_urls if u]
    if not current:
        return 0.0
    previous = {str(u) for u in previous_urls if u}
    if not previous:
        return 0.0
    hits = sum(1 for u in current if u in previous)
    return hits / len(current)


def _engines_from_results(results: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    """Per-engine hit counts from a SearXNG-shaped result list (each result's
    own ``engines`` list) -- the same tally ``providers._record_engine_health``
    keeps, duplicated here only because that one is scoped to one JSON
    response shape and this module must stay provider-agnostic."""
    engines: Dict[str, int] = {}
    for r in results:
        if not isinstance(r, Mapping):
            continue
        for eng in (r.get("engines") or []):
            key = str(eng)
            engines[key] = engines.get(key, 0) + 1
    return engines


def diversity_report(
    results: Iterable[Mapping[str, Any]],
    *,
    previous_urls: Optional[Iterable[str]] = None,
    engines_answered: Optional[Mapping[str, int]] = None,
) -> Dict[str, Any]:
    """Diversity/coverage snapshot for one batch of search ``results``.

    ``results`` is the plain list of result dicts a provider returns (``url``
    required, ``engines`` optional -- present for SearXNG, absent for a
    single-provider search like Brave/Tavily, in which case
    ``engines_answered`` lets a caller supply the "one engine" fact from
    elsewhere, e.g. the provider name itself). ``previous_urls`` is the prior
    query's URL list, when the caller is tracking one (``None`` means "no
    prior query to compare against" -- ``overlap_with_previous_query`` is then
    ``None`` too, not 0.0, because "no overlap" and "nothing to compare" are
    different facts).

    Returns a JSON-safe dict; never raises on malformed input.
    """
    result_list = [r for r in (results or []) if isinstance(r, Mapping)]
    urls = [str(r.get("url") or "") for r in result_list]
    urls = [u for u in urls if u]
    domains = unique_domains(urls)

    engines = dict(engines_answered) if engines_answered is not None else _engines_from_results(result_list)

    ratio: Optional[float] = None
    if previous_urls is not None:
        ratio = round(overlap_ratio(urls, previous_urls), 4)

    warnings: List[str] = []
    if engines and len(engines) <= 1:
        only = next(iter(engines))
        warnings.append(f"only one search engine ({only}) answered — coverage may be narrow")
    if ratio is not None and ratio > HIGH_OVERLAP_THRESHOLD:
        warnings.append(
            f"{ratio:.0%} of these URLs repeat the previous query — same ground, not new coverage"
        )

    return {
        "result_count": len(urls),
        "unique_domains": domains,
        "domain_count": len(domains),
        "engines_answered": engines,
        "overlap_with_previous_query": ratio,
        "coverage_warning": " ; ".join(warnings) if warnings else None,
    }
