"""Source independence: telling one story from five copies of it.

Deterministic and pure, in the same spirit as :mod:`src.research_citations`:
no network, no LLM, no database. Five cited pages can be the same syndicated
wire story (or the same article mirrored on five domains) rather than five
independent looks at a claim, and a report that leans on them should say so
instead of letting the reader count "[2][5][7][9][11]" as five confirmations.

Two jobs:

1. :func:`cluster_sources` groups source numbers that are, as best this module
   can tell without fetching anything new, the same underlying piece of
   reporting: the same URL once tracking parameters are stripped, the same
   wire-service copy, or near-duplicate text.
2. :func:`independence_summary` turns those clusters into the two numbers a
   reader actually wants — how many sources were cited, and how many of them
   were independent of each other — plus the specific clusters worth naming.

Like citation checking, this is honest about its limits: clustering by text
similarity only fires above a real duplication threshold and only on texts
long enough to compare meaningfully, and clustering by wire marker only fires
when the marker looks like a byline attribution, not a passing mention. A
false "these are the same source" is worse than missing a real duplicate, so
every rule here is deliberately conservative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from src.research_citations import canonical_url

# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

#: Near-duplicate text is judged on this many words at most, from the start of
#: the stored text -- enough to catch a mirrored article without letting a
#: huge page dominate the comparison cost.
MAX_WORDS_FOR_SHINGLES = 4000

#: Below this many words, a 5-word-shingle Jaccard score is noise: a two- or
#: three-sentence snippet from two unrelated short pages can share most of its
#: shingles by accident. Texts shorter than this are never clustered by
#: rule (c) (near-duplicate text alone).
MIN_WORDS_FOR_NEAR_DUPLICATE = 80

SHINGLE_SIZE = 5
NEAR_DUPLICATE_JACCARD = 0.6

#: A wire attribution marker is only read from the opening of a page, where a
#: byline lives -- a wire agency named in paragraph twelve is a citation, not
#: an attribution.
ATTRIBUTION_WINDOW_CHARS = 600

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_DASH = r"[-–—]"  # hyphen, en dash, em dash

# Attribution forms only: "(Reuters) -", "-- AP", "(EFE)", a "Europa Press"
# byline. Each pattern requires a dash or parenthesis touching the name, which
# is what a byline looks like and a body-text mention does not.
_WIRE_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "Reuters": (
        r"\(\s*Reuters\s*\)\s*" + _DASH,
        _DASH + r"\s*Reuters\b",
        r"/\s*Reuters\b",
    ),
    "AP": (
        r"\(\s*AP\s*\)\s*" + _DASH,
        r"\(\s*Associated Press\s*\)",
        _DASH + r"\s*(?:AP|Associated Press)\b",
    ),
    "AFP": (
        r"\(\s*AFP\s*\)",
        _DASH + r"\s*AFP\b",
    ),
    "EFE": (
        r"\(\s*EFE\s*\)",
        _DASH + r"\s*EFE\b",
    ),
    "Europa Press": (
        r"\bEuropa Press\b\s*" + _DASH,
        _DASH + r"\s*Europa Press\b",
    ),
    "Bloomberg": (
        r"\(\s*Bloomberg\s*\)",
        _DASH + r"\s*Bloomberg\b",
    ),
    "dpa": (
        r"\(\s*dpa\s*\)",
        _DASH + r"\s*dpa\b",
    ),
    "ANSA": (
        r"\(\s*ANSA\s*\)",
        _DASH + r"\s*ANSA\b",
    ),
    "PA Media": (
        r"\bPA Media\b",
        r"\(\s*PA\s*\)\s*" + _DASH,
    ),
    "Xinhua": (
        r"\(\s*Xinhua\s*\)",
        _DASH + r"\s*Xinhua\b",
    ),
}
_WIRE_REGEXES: Dict[str, Tuple[re.Pattern, ...]] = {
    name: tuple(re.compile(p, re.IGNORECASE) for p in patterns)
    for name, patterns in _WIRE_PATTERNS.items()
}


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _source_number(source: Any) -> int:
    if not isinstance(source, dict):
        return 0
    for key in ("number", "n"):
        if key in source:
            try:
                value = int(source[key])
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return 0


def _source_text(source: Dict[str, Any]) -> str:
    """The best text this module has for a source, whatever the caller named
    it -- SourceRegistry entries carry "evidence"/"summary", the spec that
    asked for this module speaks of "text"."""
    for key in ("text", "evidence", "summary"):
        value = source.get(key)
        if value:
            return str(value)
    return ""


def _source_url(source: Dict[str, Any]) -> str:
    value = source.get("url")
    return str(value) if value else ""


def wire_markers(text: Any) -> List[str]:
    """Wire agencies whose attribution marker appears in the opening of
    ``text``. Order is stable (declaration order above), not appearance order."""
    window = str(text or "")[:ATTRIBUTION_WINDOW_CHARS]
    found = []
    for name, patterns in _WIRE_REGEXES.items():
        for pattern in patterns:
            if pattern.search(window):
                found.append(name)
                break
    return found


def _words(text: str, limit: int) -> List[str]:
    return _WORD_RE.findall(text.lower())[:limit]


def _shingles(words: Sequence[str], size: int) -> FrozenSet[Tuple[str, ...]]:
    if len(words) < size:
        return frozenset()
    return frozenset(tuple(words[i:i + size]) for i in range(len(words) - size + 1))


def _jaccard(a: FrozenSet[Any], b: FrozenSet[Any]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    if not union:
        return 0.0
    return len(a & b) / union


def is_near_duplicate(text_a: Any, text_b: Any) -> bool:
    """True when two texts are, word-shingle-wise, close enough to be the same
    piece of writing. Texts shorter than :data:`MIN_WORDS_FOR_NEAR_DUPLICATE`
    words never qualify -- there is not enough material to tell a real
    duplicate from two short pages that happen to share phrasing."""
    words_a = _words(str(text_a or ""), MAX_WORDS_FOR_SHINGLES)
    words_b = _words(str(text_b or ""), MAX_WORDS_FOR_SHINGLES)
    if len(words_a) < MIN_WORDS_FOR_NEAR_DUPLICATE or len(words_b) < MIN_WORDS_FOR_NEAR_DUPLICATE:
        return False
    shingles_a = _shingles(words_a, SHINGLE_SIZE)
    shingles_b = _shingles(words_b, SHINGLE_SIZE)
    return _jaccard(shingles_a, shingles_b) >= NEAR_DUPLICATE_JACCARD


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

# Reason ranks, highest wins when a cluster's members are linked by more than
# one kind of evidence (e.g. A-B share a URL, B-C are a wire near-duplicate):
# a same_url pair is certain, a wire-plus-near-duplicate pair is close to it,
# a bare near-duplicate is the weakest of the three.
_REASON_RANK = {"near_duplicate": 0, "wire": 1, "same_url": 2}


@dataclass(frozen=True)
class Cluster:
    """One underlying piece of reporting, as numbered sources.

    ``representative`` is the lowest source number in the cluster.
    ``members`` is every source number in it, sorted, including the
    representative -- a cluster of one source is a source with nothing
    duplicating it. ``reason`` is ``""`` for a singleton, ``"same_url"``,
    ``"wire:<Name>"`` or ``"near_duplicate"`` for a cluster of more than one.
    """

    representative: int
    members: Tuple[int, ...]
    reason: str

    @property
    def weight(self) -> float:
        """Each member's share of independent evidence: 1 for a singleton,
        1/len(members) when several numbered sources are one underlying
        story, so summing weights counts stories, not citations."""
        return 1.0 / len(self.members) if self.members else 0.0


def _cluster_reason(members: Tuple[int, ...],
                    pair_reasons: Dict[Tuple[int, int], Tuple[str, str]]) -> str:
    if len(members) <= 1:
        return ""
    member_set = set(members)
    best_kind: Optional[str] = None
    best_detail = ""
    best_rank = -1
    for (a, b), (kind, detail) in pair_reasons.items():
        if a in member_set and b in member_set:
            rank = _REASON_RANK.get(kind, -1)
            if rank > best_rank:
                best_rank, best_kind, best_detail = rank, kind, detail
    if best_kind is None:
        return "near_duplicate"  # unioned transitively; no direct pair recorded
    if best_kind == "wire":
        return f"wire:{best_detail}"
    return best_kind


def cluster_sources(sources: Sequence[Any]) -> List[Cluster]:
    """Group source numbers that look like the same underlying piece of
    reporting.

    ``sources`` is anything shaped like the entries :class:`SourceRegistry`
    keeps (a ``dict`` with a number under ``"n"`` or ``"number"``, a
    ``"url"``, and text under ``"text"``, ``"evidence"`` or ``"summary"``).
    Every source with a positive number gets a cluster of at least itself,
    stably ordered by that number, so the result is total and deterministic.
    """
    entries = []
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        number = _source_number(source)
        if number <= 0:
            continue
        url = _source_url(source)
        entries.append({
            "n": number,
            "canon": canonical_url(url) if url else "",
            "text": _source_text(source),
            "wires": wire_markers(_source_text(source)),
        })
    entries.sort(key=lambda e: e["n"])

    parent: Dict[int, int] = {e["n"]: e["n"] for e in entries}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        root_a, root_b = find(a), find(b)
        if root_a == root_b:
            return
        # Lower number stays root, so the representative is always the lowest
        # member and the ordering never depends on comparison order.
        if root_a < root_b:
            parent[root_b] = root_a
        else:
            parent[root_a] = root_b

    pair_reasons: Dict[Tuple[int, int], Tuple[str, str]] = {}

    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i], entries[j]
            key = (a["n"], b["n"])
            reason: Optional[Tuple[str, str]] = None
            if a["canon"] and a["canon"] == b["canon"]:
                reason = ("same_url", "")
            else:
                common_wires = sorted(set(a["wires"]) & set(b["wires"]))
                if common_wires and is_near_duplicate(a["text"], b["text"]):
                    reason = ("wire", common_wires[0])
                elif is_near_duplicate(a["text"], b["text"]):
                    reason = ("near_duplicate", "")
            if reason is not None:
                union(a["n"], b["n"])
                pair_reasons[key] = reason

    groups: Dict[int, List[int]] = {}
    for entry in entries:
        groups.setdefault(find(entry["n"]), []).append(entry["n"])

    clusters = []
    for root in sorted(groups):
        members = tuple(sorted(groups[root]))
        clusters.append(Cluster(representative=members[0], members=members,
                                reason=_cluster_reason(members, pair_reasons)))
    return clusters


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def independence_summary(sources: Sequence[Any],
                         cited_numbers: Sequence[Any]) -> Dict[str, Any]:
    """How many of the cited sources are actually independent of each other.

    ``total_cited`` is the count of distinct cited source numbers.
    ``independent_cited`` is the number of distinct clusters those numbers
    fall into -- five cited numbers that are all the same wire story count as
    one. ``clusters`` lists, sorted by representative, every cluster that has
    more than one cited member: the ones worth naming in a legend.
    """
    clusters = cluster_sources(sources)
    cluster_by_number: Dict[int, Cluster] = {}
    for cluster in clusters:
        for member in cluster.members:
            cluster_by_number[member] = cluster

    cited: set = set()
    for value in cited_numbers or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            cited.add(number)

    roots: set = set()
    for number in cited:
        cluster = cluster_by_number.get(number)
        roots.add(cluster.representative if cluster is not None else number)

    named_clusters = []
    for cluster in clusters:
        cited_members = [m for m in cluster.members if m in cited]
        if len(cited_members) > 1:
            named_clusters.append({
                "representative": cluster.representative,
                "members": cited_members,
                "reason": cluster.reason,
            })
    named_clusters.sort(key=lambda c: c["representative"])

    return {
        "total_cited": len(cited),
        "independent_cited": len(roots),
        "clusters": named_clusters,
    }


# ---------------------------------------------------------------------------
# Legend line
# ---------------------------------------------------------------------------

_SOURCES_WORD = {
    "en": "Sources", "es": "Las fuentes", "fr": "Les sources", "de": "Die Quellen",
    "pt": "As fontes", "it": "Le fonti",
}
_SOURCE_WORD_SINGULAR = {
    "en": "Source", "es": "La fuente", "fr": "La source", "de": "Die Quelle",
    "pt": "A fonte", "it": "La fonte",
}
_INTRO_TEMPLATE = {
    "en": "{word} {members} are {desc}",
    "es": "{word} {members} son {desc}",
    "fr": "{word} {members} sont {desc}",
    "de": "{word} {members} sind {desc}",
    "pt": "{word} {members} são {desc}",
    "it": "{word} {members} sono {desc}",
}
_REASON_PHRASES = {
    "en": {"same_url": "the same page", "wire": "the same wire story ({name})",
           "dup": "near-duplicate coverage"},
    "es": {"same_url": "la misma página", "wire": "la misma noticia de agencia ({name})",
           "dup": "contenido casi idéntico"},
    "fr": {"same_url": "la même page", "wire": "la même dépêche d'agence ({name})",
           "dup": "un contenu quasi identique"},
    "de": {"same_url": "dieselbe Seite", "wire": "dieselbe Agenturmeldung ({name})",
           "dup": "nahezu identischer Inhalt"},
    "pt": {"same_url": "a mesma página", "wire": "a mesma notícia de agência ({name})",
           "dup": "conteúdo quase idêntico"},
    "it": {"same_url": "la stessa pagina", "wire": "la stessa notizia d'agenzia ({name})",
           "dup": "contenuto quasi identico"},
}
_TOTALS_TEMPLATE = {
    "en": "{total} cited, {independent} independent.",
    "es": "{total} citadas, {independent} independientes.",
    "fr": "{total} citées, {independent} indépendantes.",
    "de": "{total} zitiert, {independent} unabhängig.",
    "pt": "{total} citadas, {independent} independentes.",
    "it": "{total} citate, {independent} indipendenti.",
}


def _cluster_description(reason: str, phrases: Dict[str, str]) -> str:
    if reason == "same_url":
        return phrases["same_url"]
    if reason.startswith("wire:"):
        return phrases["wire"].format(name=reason.split(":", 1)[1])
    return phrases["dup"]


def independence_legend_line(summary: Dict[str, Any], language: str = "en") -> str:
    """One honest sentence naming the cited sources that are not independent
    of each other, or "" when every cited source is independent -- there is
    nothing to say, and printing the line anyway would flatter the report for
    a problem it does not have."""
    clusters = (summary or {}).get("clusters") or []
    if not clusters:
        return ""
    lang = (language or "en").lower()
    plural_word = _SOURCES_WORD.get(lang, _SOURCES_WORD["en"])
    singular_word = _SOURCE_WORD_SINGULAR.get(lang, _SOURCE_WORD_SINGULAR["en"])
    intro = _INTRO_TEMPLATE.get(lang, _INTRO_TEMPLATE["en"])
    phrases = _REASON_PHRASES.get(lang, _REASON_PHRASES["en"])
    totals = _TOTALS_TEMPLATE.get(lang, _TOTALS_TEMPLATE["en"])

    parts = []
    for index, cluster in enumerate(clusters):
        members = cluster.get("members") or []
        member_text = ", ".join(f"[{n}]" for n in members)
        desc = _cluster_description(str(cluster.get("reason") or ""), phrases)
        if index == 0:
            word = plural_word if len(members) != 1 else singular_word
        else:
            word = (plural_word if len(members) != 1 else singular_word).lower()
        parts.append(intro.format(word=word, members=member_text, desc=desc))

    total = int((summary or {}).get("total_cited") or 0)
    independent = int((summary or {}).get("independent_cited") or 0)
    return "; ".join(parts) + ": " + totals.format(total=total, independent=independent)
