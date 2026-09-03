"""Positional near-duplicate detection — `franken_overlap`, without embeddings.

The provenance graph (``src/provenance_graph.py``) may only draw edges that
trace back to something already stored as declared truth. "These two memories
say the same thing" is exactly the kind of claim a language model is happy to
invent, so this module never asks one: it finds shared text POSITIONALLY, with
q-grams and winnowing, and then **verifies every candidate span with an exact
substring comparison before reporting it**. A span that does not compare equal
character for character is dropped, not reported with a lower score.

How it works
------------
1. **Normalize.** Lowercase, collapse every whitespace run to one space, strip.
   Two passages that differ only in indentation are the same passage.
2. **q-grams.** Every ``k``-character window of the normalized string, with the
   offset it starts at.
3. **Winnowing** (Schleimer, Wilkerson & Aiken 2003) with window ``w``: in each
   window of ``w`` consecutive q-gram hashes pick the minimum, **rightmost on
   ties**, and record it only when it is not the one already recorded. That is
   the standard scheme, and it buys the two properties this needs: identical
   text ALWAYS shares fingerprints (so an exact duplicate can never be missed),
   and the fingerprint set stays sparse (~1 per ``w`` positions) so comparing
   300 memories is cheap.
4. **Diagonal vote.** Two fingerprints with the same hash are a candidate match
   at diagonal ``offset_a - offset_b``. Matches on one diagonal are the same
   alignment; matches on different diagonals are different alignments and are
   NEVER merged — that is what stops a phrase repeated twice in one document
   from being reported as one long shared span.
5. **Literal verification.** Each candidate anchor is checked
   (``norm_a[pa:pa+k] == norm_b[pb:pb+k]``) and then extended left and right
   character by character while the two strings stay equal. What is reported is
   therefore the MAXIMAL exactly-equal region through that anchor. A hash
   collision, or a diagonal that only nearly lines up, produces nothing.

Hashes are 64-bit (``hashlib.blake2b(digest_size=8)``). The report this feature
comes from specifies 128; 64 is enough here and halves the memory a fingerprint
set costs. The reason it is enough: a hash is never trusted on its own — every
span it proposes is confirmed by an exact string compare, so a collision costs
one wasted comparison, never a wrong answer.

Offsets index the NORMALIZED string, i.e. the value :func:`normalize` returns,
because that is the string the verification compares; :func:`span_text` slices
it. Reported spans satisfy, by construction::

    span_text(a, a0, a1) == span_text(b, b0, b1)

Pure stdlib, pure functions, and nothing here raises: a caller on a hot path
gets an empty result instead of an exception.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Sequence, Tuple

__all__ = [
    "normalize", "qgrams", "fingerprints", "overlap", "span_text",
    "find_duplicates", "DEFAULT_K", "DEFAULT_W", "DEFAULT_THRESHOLD",
]

DEFAULT_K = 5                 # q-gram length
DEFAULT_W = 4                 # winnowing window, in q-grams
DEFAULT_THRESHOLD = 0.6       # find_duplicates: report at or above this ratio

# Work bounds. Every one of them degrades the ANSWER (fewer spans), never the
# call: this runs behind an HTTP read and inside graph building.
MAX_TEXT_CHARS = 20_000       # longer inputs are compared on their first 20k
MAX_POSITIONS_PER_HASH = 64   # a hash that repeats more than this is boilerplate
MAX_PAIRS = 50_000            # candidate (a, b) fingerprint matches examined
MAX_SPANS = 200               # verified spans reported for one pair
MAX_ITEMS = 300               # items find_duplicates will compare pairwise

_SPAN = Tuple[Tuple[int, int], Tuple[int, int]]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def normalize(text: Any) -> str:
    """Lowercased, whitespace-collapsed, stripped. Never raises."""
    try:
        return " ".join(str(text or "").split()).lower()[:MAX_TEXT_CHARS]
    except Exception:  # noqa: BLE001 - a __str__ that explodes is empty text
        return ""


def span_text(text: Any, start: int, end: int) -> str:
    """The slice a reported span names, in normalized coordinates."""
    try:
        norm = normalize(text)
        start = max(0, int(start))
        end = min(len(norm), int(end))
        return norm[start:end] if end > start else ""
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# q-grams, hashes and winnowing
# ---------------------------------------------------------------------------


def qgrams(text: Any, k: int = DEFAULT_K) -> List[Tuple[int, str]]:
    """``[(start offset, k-gram), ...]`` over :func:`normalize`'s output.

    Text shorter than ``k`` yields nothing at all — there is no window to
    compare, and a "near-duplicate" of a three-character string is noise.
    """
    try:
        k = max(1, int(k))
    except (TypeError, ValueError):
        k = DEFAULT_K
    norm = normalize(text)
    if len(norm) < k:
        return []
    return [(i, norm[i:i + k]) for i in range(len(norm) - k + 1)]


def _hash(gram: str) -> int:
    """64-bit blake2b of one q-gram. See the module docstring for why 64 and
    not the 128 the report specifies: no hash is ever trusted on its own."""
    return int.from_bytes(
        hashlib.blake2b(gram.encode("utf-8", "replace"), digest_size=8).digest(),
        "big",
    )


def _winnow(hashes: Sequence[int], w: int) -> List[Tuple[int, int]]:
    """Standard winnowing: one ``(hash, offset)`` per window minimum.

    In each window of ``w`` consecutive hashes take the minimum and, on a tie,
    the RIGHTMOST occurrence of it; record it only when it is not the position
    already recorded. Identical text therefore always selects identical
    fingerprints, which is the property the whole scheme rests on.
    """
    n = len(hashes)
    if n == 0:
        return []
    if n <= w:
        # One short window: the rightmost minimum of the whole thing.
        best = 0
        for i in range(1, n):
            if hashes[i] <= hashes[best]:
                best = i
        return [(hashes[best], best)]
    out: List[Tuple[int, int]] = []
    last = -1
    for start in range(0, n - w + 1):
        best = start
        for i in range(start + 1, start + w):
            if hashes[i] <= hashes[best]:      # <= ⇒ rightmost wins a tie
                best = i
        if best != last:
            out.append((hashes[best], best))
            last = best
    return out


def fingerprints(text: Any, k: int = DEFAULT_K, w: int = DEFAULT_W) -> List[Tuple[int, int]]:
    """The winnowed ``[(hash, offset), ...]`` fingerprint set of one string."""
    try:
        w = max(1, int(w))
    except (TypeError, ValueError):
        w = DEFAULT_W
    grams = qgrams(text, k)
    if not grams:
        return []
    return _winnow([_hash(gram) for _, gram in grams], w)


# ---------------------------------------------------------------------------
# Overlap
# ---------------------------------------------------------------------------


class _Prepared:
    """One side of a comparison, computed once (find_duplicates reuses it)."""

    __slots__ = ("norm", "fps", "by_hash", "hashes")

    def __init__(self, text: Any, k: int, w: int) -> None:
        self.norm = normalize(text)
        self.fps = fingerprints(text, k, w)
        self.by_hash: Dict[int, List[int]] = {}
        for value, offset in self.fps:
            bucket = self.by_hash.setdefault(value, [])
            if len(bucket) < MAX_POSITIONS_PER_HASH:
                bucket.append(offset)
        self.hashes = frozenset(self.by_hash)


def _prepare(text: Any, k: int, w: int) -> _Prepared:
    return _Prepared(text, k, w)


def _coverage(spans: Iterable[Tuple[int, int]]) -> int:
    """Length of the union of a set of intervals (spans may overlap when two
    different diagonals both align part of the same text)."""
    merged: List[List[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(end - start for start, end in merged)


def _verified_spans(a: str, b: str, pa: int, pb: int, k: int) -> Tuple[int, int, int, int]:
    """Extend an anchor to the maximal literally-equal region, or (0,0,0,0).

    The anchor itself is compared first: two fingerprints sharing a hash are a
    CANDIDATE, never a match. Extension is character-by-character equality, so
    what comes back is exact by construction.
    """
    if a[pa:pa + k] != b[pb:pb + k] or len(a[pa:pa + k]) < k:
        return (0, 0, 0, 0)
    start_a, start_b = pa, pb
    while start_a > 0 and start_b > 0 and a[start_a - 1] == b[start_b - 1]:
        start_a -= 1
        start_b -= 1
    end_a, end_b = pa + k, pb + k
    while end_a < len(a) and end_b < len(b) and a[end_a] == b[end_b]:
        end_a += 1
        end_b += 1
    return (start_a, end_a, start_b, end_b)


def _overlap_prepared(pa: _Prepared, pb: _Prepared, k: int) -> Dict[str, Any]:
    a, b = pa.norm, pb.norm
    if len(a) < k or len(b) < k:
        return {"ratio": 0.0, "spans": []}

    # Candidate matches, grouped by diagonal. Two matches on ONE diagonal are
    # the same alignment; two diagonals are two alignments and never merge.
    diagonals: Dict[int, List[Tuple[int, int]]] = {}
    pairs = 0
    for value, offset_a in pa.fps:
        for offset_b in pb.by_hash.get(value, ()):
            diagonals.setdefault(offset_a - offset_b, []).append((offset_a, offset_b))
            pairs += 1
            if pairs >= MAX_PAIRS:
                break
        if pairs >= MAX_PAIRS:
            break

    spans: List[_SPAN] = []
    seen: set = set()
    for diagonal in sorted(diagonals):
        emitted: List[Tuple[int, int]] = []          # a-intervals already covered here
        for offset_a, offset_b in sorted(diagonals[diagonal]):
            if any(start <= offset_a < end for start, end in emitted):
                continue                              # already inside a verified span
            start_a, end_a, start_b, end_b = _verified_spans(a, b, offset_a, offset_b, k)
            if end_a <= start_a:
                continue                              # unverified: report nothing
            emitted.append((start_a, end_a))
            key = ((start_a, end_a), (start_b, end_b))
            if key in seen:
                continue
            seen.add(key)
            spans.append(key)
            if len(spans) >= MAX_SPANS:
                break
        if len(spans) >= MAX_SPANS:
            break

    if not spans:
        return {"ratio": 0.0, "spans": []}
    covered_a = _coverage((s[0][0], s[0][1]) for s in spans)
    covered_b = _coverage((s[1][0], s[1][1]) for s in spans)
    total = len(a) + len(b)
    ratio = ((covered_a + covered_b) / total) if total else 0.0
    spans.sort()
    return {"ratio": round(min(1.0, max(0.0, ratio)), 6), "spans": spans}


def overlap(a: Any, b: Any, *, k: int = DEFAULT_K, w: int = DEFAULT_W) -> Dict[str, Any]:
    """``{"ratio": float, "spans": [((a0, a1), (b0, b1)), ...]}``.

    ``ratio`` is symmetric coverage — the share of the two normalized strings
    that a VERIFIED span covers, so identical text is 1.0 and unrelated text is
    0.0 with no spans at all. Offsets index :func:`normalize`'s output, and
    every reported pair satisfies ``span_text(a, a0, a1) == span_text(b, b0, b1)``.

    Empty, too-short or unusable input answers ``{"ratio": 0.0, "spans": []}``.
    Never raises.
    """
    try:
        k = max(1, int(k))
    except (TypeError, ValueError):
        k = DEFAULT_K
    try:
        return _overlap_prepared(_prepare(a, k, w), _prepare(b, k, w), k)
    except Exception:  # noqa: BLE001 - a detector may never break its caller
        return {"ratio": 0.0, "spans": []}


# ---------------------------------------------------------------------------
# find_duplicates
# ---------------------------------------------------------------------------


def find_duplicates(
    items: Any,
    threshold: float = DEFAULT_THRESHOLD,
    *,
    k: int = DEFAULT_K,
    w: int = DEFAULT_W,
    max_items: int = MAX_ITEMS,
) -> List[Dict[str, Any]]:
    """Pairs of ``{"id", "text"}`` items whose verified overlap ratio is at or
    above ``threshold``.

    Returns ``[{"a", "b", "ratio", "spans"}]`` with ``a < b`` inside a pair and
    the list sorted by ``(-ratio, a, b)`` — the same input always produces the
    same output, in the same order. Fingerprint sets are computed once per item
    and a pair sharing no fingerprint at all is skipped without a comparison.

    Never raises: a malformed item is skipped, and any other failure yields an
    empty list.
    """
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = DEFAULT_THRESHOLD
    try:
        max_items = max(0, int(max_items))
    except (TypeError, ValueError):
        max_items = MAX_ITEMS
    try:
        k = max(1, int(k))
    except (TypeError, ValueError):
        k = DEFAULT_K

    try:
        rows: List[Tuple[str, _Prepared]] = []
        seen_ids: set = set()
        for item in (items if isinstance(items, (list, tuple)) else []):
            if not isinstance(item, dict):
                continue
            ident = str(item.get("id") or "")
            if not ident or ident in seen_ids:
                continue
            prepared = _prepare(item.get("text"), k, w)
            if len(prepared.norm) < k or not prepared.fps:
                continue
            seen_ids.add(ident)
            rows.append((ident, prepared))
        rows.sort(key=lambda pair: pair[0])
        rows = rows[:max_items]

        out: List[Dict[str, Any]] = []
        for i in range(len(rows)):
            id_a, prep_a = rows[i]
            for j in range(i + 1, len(rows)):
                id_b, prep_b = rows[j]
                if not (prep_a.hashes & prep_b.hashes):
                    continue                       # nothing in common: no compare
                result = _overlap_prepared(prep_a, prep_b, k)
                if result["ratio"] < threshold or not result["spans"]:
                    continue
                out.append({"a": id_a, "b": id_b, "ratio": result["ratio"],
                            "spans": result["spans"]})
        out.sort(key=lambda row: (-row["ratio"], row["a"], row["b"]))
        return out
    except Exception:  # noqa: BLE001 - never break the caller
        return []
