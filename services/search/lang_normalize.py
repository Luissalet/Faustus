"""Unicode-normalized, accent-insensitive search with position correspondence
(LANG-02).

``docs/spec/v2/backlog.json`` (LANG-02): search must work whether or not a
query carries the diacritics the indexed text does ("tilde opcional"), and
detecting a query's language is worth doing when it changes what gets
searched. The acceptance criterion is exact: normalizing text FOR MATCHING
must never modify a filename, and a match's reported position must never
drift from where the text actually is -- a caller locating a diff range or
an edit point off a normalized search must land on the real offsets.

:func:`strip_diacritics` is the seam that makes this possible: it folds
case and diacritics ONE INPUT CHARACTER AT A TIME, so the folded string is
always exactly as long as the original and index *i* of one is always
index *i* of the other. Every other function here builds on that
1:1 guarantee instead of re-deriving offsets by any fuzzier means.
"""

from __future__ import annotations

import unicodedata
from typing import List, Optional, Tuple


def strip_diacritics(text: str) -> str:
    """Fold diacritics and case for MATCHING, one character in, one
    character out -- ``len(strip_diacritics(text)) == len(text)`` always,
    so an offset into the result is always a valid offset into `text`
    itself, unchanged.

    Never call this on a path/filename and use the result as the name --
    it is a matching key, not a normalized identifier; this module never
    touches file or diff content, only returns positions a caller applies
    to the ORIGINAL text.
    """
    out_chars: List[str] = []
    for ch in text:
        decomposed = unicodedata.normalize("NFD", ch)
        base = "".join(c for c in decomposed if not unicodedata.combining(c))
        # A rare multi-codepoint decomposition keeps only its first base
        # character, so the 1:1 length guarantee never breaks.
        out_chars.append((base[:1] or ch).lower())
    return "".join(out_chars)


def find_all_normalized(haystack: str, needle: str) -> List[Tuple[int, int]]:
    """Every match of `needle` in `haystack`, compared with diacritics and
    case folded, returned as ``(start, end)`` offsets into the ORIGINAL,
    UNMODIFIED `haystack` -- never the folded copy.

    This is LANG-02's acceptance criterion made a function: a normalized
    search must not shift where a caller thinks a match sits in the real
    text (a diff range, an edit point). Overlapping matches are not
    returned (a match consumes its own span before the next search
    starts), matching how `str.find` in a loop is normally used.
    """
    if not needle:
        return []
    folded_hay = strip_diacritics(haystack)
    folded_needle = strip_diacritics(needle)
    matches: List[Tuple[int, int]] = []
    start = 0
    length = len(folded_needle)
    while True:
        idx = folded_hay.find(folded_needle, start)
        if idx == -1:
            break
        matches.append((idx, idx + length))
        start = idx + length
    return matches


def contains_normalized(haystack: str, needle: str) -> bool:
    return bool(find_all_normalized(haystack, needle))


def locate_normalized(haystack: str, needle: str) -> Optional[Tuple[int, int]]:
    """The FIRST normalized match's ``(start, end)`` in `haystack`, or
    ``None``. Convenience wrapper around :func:`find_all_normalized` for a
    caller that only needs one edit point."""
    matches = find_all_normalized(haystack, needle)
    return matches[0] if matches else None


def detect_query_language(text: str) -> Optional[str]:
    """The language `text` (typically a short search query) most likely
    reads as, or ``None`` when it carries no real signal.

    Reuses `src.research_citations.language_signal` (the same authority
    `src.reply_language` pins the reply language off) rather than a second,
    search-specific language detector (rule 4).
    """
    from src.research_citations import language_signal

    code, score = language_signal(text)
    return code if score >= 1.0 else None


def multilingual_query_variants(query: str) -> List[str]:
    """`query`, plus a diacritic-folded variant when it differs -- never a
    replacement, always additional (LANG-02: "resultados sin sesgo de
    idioma"; a search backend that scores accented and unaccented terms
    differently should see both instead of only whichever the caller
    happened to type). Order-preserving and deduplicated.
    """
    variants = [query]
    folded = strip_diacritics(query)
    if folded and folded != query and folded not in variants:
        variants.append(folded)
    return variants


def should_try_english_variant(query_language: Optional[str]) -> bool:
    """Whether a caller should ALSO try an English phrasing of a query
    whose detected language is `query_language` -- true for any real,
    non-English signal, false for English itself or "no real signal"
    (``None``, from :func:`detect_query_language`), which must not be read
    as "needs an English variant" (it means the opposite: nothing settled
    a language at all).
    """
    return bool(query_language) and query_language != "en"
