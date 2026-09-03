"""Two-tier search: fast first, better later, never an error (`frankensearch`).

The rule this module exists to enforce is that **search degrades, it does not
fail**. Every semantic lane in this app is optional — ChromaDB may not be
installed, fastembed may not have downloaded its model, an embedding endpoint
may be a machine that is off. A search that answers "500" when the optional
half is missing is worse than no feature at all, because the user cannot tell
a broken index from an empty one.

So there are two tiers and three answers:

============  ==================================================  ==========
``tier``      what ran                                            ``degraded``
============  ==================================================  ==========
``reranked``  any tier below, reordered by a cross-encoder        unchanged
``refined``   tier 1, with a real embedder mixed over it (α=0.7)  ``False``
``hybrid``    BM25-lite + :mod:`src.hash_embed`, fused by RRF     ``True``
``lexical``   BM25-lite alone (nothing could be vectorised)       ``True``
============  ==================================================  ==========

``degraded`` answers one question — "is this the best this machine can do?" —
and ``tier`` says how far down it went. A caller that shows results without
showing the tier is lying by omission, which is why every surface built on
this prints it.

``reranked`` is the one tier that is *opt-in*: it needs a cross-encoder over
HTTP, and this module does no I/O of its own (see the closing paragraph), so
it only happens when the caller passes ``reranker``. It is a stage on top of
the others rather than a replacement for one, which is why it leaves
``degraded`` alone — ``degraded`` still answers about the retrieval lanes,
and a hybrid retrieval that was reranked is still a retrieval with no real
embedder in it. Whether the rerank stage ran, and if not why not, is its own
field: ``rerank_reason``, one of the named reasons in :mod:`src.rerank`.

What is different from the expert corpus search
-----------------------------------------------
``services/experts.py`` already fuses BM25 with a vector store by RRF over one
expert's chunks. This module is not a copy of it and does not replace it; it
is the *generic* version, over any ``[{"id", "text", **meta}]`` corpus, with
two things that one does not have:

* **tier 1 is itself two lanes.** Because :mod:`src.hash_embed` needs no model
  and no network, the "no embedder at all" case is still a fused hybrid rather
  than bare BM25. The expert search's ``lexical`` floor becomes this module's
  ``hybrid`` floor.
* **a third tier.** When a real embedder does exist it is *mixed over* tier 1
  rather than fused as an equal, because it is not an equal:
  ``score = α·refined + (1−α)·tier1`` with α = 0.7.

Constants are the report's, not invented here: Reciprocal Rank Fusion is
``Σ 1/(60 + rank)`` and the tier-2 mix is α = 0.7.

Why the hash lane is fused at half weight, measured
---------------------------------------------------
RRF assumes its lanes are **independent evidence**: agreement between two
different views of a document is a real signal, which is why ``Σ 1/(60+rank)``
lets a document both lanes like beat a document only one of them ranked first.
BM25 and a hash-vector cosine are not independent — they read the *same
tokens*. The hash lane is the same evidence without IDF, and with a length
normalisation that measures how much of the *document* the query covers rather
than the other way round. Weighted equally, the weaker view of the same
evidence demotes the stronger one.

Measured on this repo's ``BUILTIN_TOOL_DESCRIPTIONS`` (99 long, deliberately
cross-referencing documents) over 21 queries — 13 near-verbatim, 8 paraphrased
— fusing at ``Σ w/(60+rank)``:

    ==========  ======  ======  ========
    hash w      top-1   top-3   recall@8
    ==========  ======  ======  ========
    1.0         10/21   14/21   14/21
    0.5         10/21   14/21   16/21
    0.1         12/21   15/21   16/21
    0.0 (BM25)  13/21   16/21   16/21
    ==========  ======  ======  ========

On the SHORT-document corpus this feature actually ranks — one user's own chat
messages — the weight changes nothing at all: 7/7 top-1 at every value from
1.0 down to 0.0. So the weight only ever costs or saves on long documents, and
:data:`HASH_WEIGHT` is 0.5, the largest value at which recall@8 on long
documents is the same as BM25's own. Four other repairs were measured and
discarded because they moved nothing: a best-window comparison, a similarity
floor from 0.05 to 0.40, IDF-weighted buckets, and dropping stop words from
the shared tokenizer.

The spec's own tier 2 already says unequal lanes get unequal weights (α=0.7
for the real embedder). This is that same rule applied one level down, and the
constant 60 is untouched.

What the lane is FOR is the case where the alternative is nothing at all.
``src.tool_index.retrieve()`` returned an empty list when no embedder could be
built, dropping every agent turn to keyword-only tool selection; the same path
now answers 16/21 recall@8. A lane that turns 0 into 16 is worth having even
where it does not beat the lane beside it.

The honest summary, which every surface prints: this is ``degraded: true``.

Everything here is pure: no I/O, no globals, no clock except the injectable
one, and :func:`search` catches every exception it can possibly meet. Stdlib
only.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from src import hash_embed

logger = logging.getLogger(__name__)

__all__ = [
    "RRF_K",
    "ALPHA",
    "TIERS",
    "RERANK_HEAD",
    "bm25_scores",
    "rrf",
    "search",
    "snippet",
]

# The report's Reciprocal Rank Fusion constant. Σ w/(60 + rank).
RRF_K = 60.0
# Weight of the real embedder when it exists. It is better than tier 1, not
# infallible, so 0.7 rather than 1.0 — a lexical exact match still counts.
ALPHA = 0.7
# Weight of the hash lane inside tier 1. NOT a tuning knob: see the module
# docstring for the measurement. BM25 and the hash cosine read the same
# tokens, so they are not the independent evidence RRF assumes; 0.5 is the
# largest weight at which the fusion's recall on long documents matches
# BM25's own, and it changes nothing on short ones.
HASH_WEIGHT = 0.5
BM25_WEIGHT = 1.0

# BM25-lite parameters, the same ones src/memory_engine.py and
# services/experts.py use, so the three lexical lanes rank alike.
BM25_K1 = 1.5
BM25_B = 0.75

TIER_LEXICAL = "lexical"
TIER_HYBRID = "hybrid"
TIER_REFINED = "refined"
TIER_RERANKED = "reranked"
TIERS = (TIER_LEXICAL, TIER_HYBRID, TIER_REFINED, TIER_RERANKED)

# How many fused hits go to the cross-encoder. Deliberately larger than a
# typical k: the whole value of a reranker is promoting a document fusion put
# at rank 20, which cannot happen if it only ever sees the top 8.
RERANK_HEAD = 30

DEFAULT_K = 8
MAX_K = 200
# How much of a document the semantic lanes see. A 200 KB message would
# otherwise dominate the embedding cost of a whole corpus.
MAX_DOC_CHARS = 20_000
SNIPPET_CHARS = 240


# ---------------------------------------------------------------------------
# Tier 1, lane A: BM25-lite
# ---------------------------------------------------------------------------


def bm25_scores(query: Any, docs: Sequence[Tuple[str, Any]]) -> Dict[str, float]:
    """BM25-lite over :func:`src.hash_embed.tokens`, normalised to 0..1.

    The same tokenizer as the vector lane on purpose (see the module docstring
    of :mod:`src.hash_embed`): fusing rankings built from two different
    vocabularies fuses two different questions.
    """
    query_terms = hash_embed.tokens(query)
    if not query_terms or not docs:
        return {}
    tokenised = [(str(doc_id), hash_embed.tokens(text)) for doc_id, text in docs]
    total_docs = len(tokenised)
    lengths = [len(terms) for _, terms in tokenised]
    avgdl = (sum(lengths) / total_docs) if total_docs else 0.0
    if avgdl <= 0:
        return {}
    df: Dict[str, int] = {}
    for _, terms in tokenised:
        for term in set(terms):
            df[term] = df.get(term, 0) + 1
    scores: Dict[str, float] = {}
    for doc_id, terms in tokenised:
        if not terms:
            continue
        length = len(terms)
        total = 0.0
        for term in set(query_terms):
            freq = terms.count(term)
            if not freq:
                continue
            n_q = df.get(term, 0)
            idf = math.log(1.0 + (total_docs - n_q + 0.5) / (n_q + 0.5))
            total += idf * (freq * (BM25_K1 + 1.0)) / (
                freq + BM25_K1 * (1.0 - BM25_B + BM25_B * length / avgdl))
        if total > 0:
            scores[doc_id] = total
    top = max(scores.values()) if scores else 0.0
    return {doc_id: value / top for doc_id, value in scores.items()} if top > 0 else {}


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------


def rrf(*rankings: Sequence[str], k: float = RRF_K,
        weights: Optional[Sequence[float]] = None) -> Dict[str, float]:
    """Reciprocal Rank Fusion: ``Σ w/(k + rank)``, rank starting at 1.

    Rank-based on purpose — the two lanes' raw scores are on different scales
    (a normalised BM25 and a cosine) and averaging them would just weight
    whichever happens to spread wider.

    ``weights`` defaults to 1.0 per ranking, which is the report's plain
    ``Σ 1/(60 + rank)``. A caller passes weights when its lanes are not
    independent evidence; see :data:`HASH_WEIGHT` and the module docstring.
    """
    fused: Dict[str, float] = {}
    for index, ranking in enumerate(rankings):
        try:
            weight = float(weights[index]) if weights is not None else 1.0
        except (TypeError, ValueError, IndexError):
            weight = 1.0
        for rank_index, doc_id in enumerate(ranking or (), start=1):
            doc_id = str(doc_id)
            fused[doc_id] = fused.get(doc_id, 0.0) + weight / (k + rank_index)
    return fused


def _ordered(scores: Dict[str, float]) -> List[str]:
    """Best first, ties broken by id — a total, reproducible order."""
    return sorted(scores, key=lambda doc_id: (-scores[doc_id], doc_id))


def _normalised(scores: Dict[str, float]) -> Dict[str, float]:
    """Scale to 0..1 by the top value; an empty or non-positive map is {}."""
    if not scores:
        return {}
    top = max(scores.values())
    if top <= 0:
        return {}
    return {doc_id: value / top for doc_id, value in scores.items()}


# ---------------------------------------------------------------------------
# Tier 2: whatever real embedder the caller has
# ---------------------------------------------------------------------------


def _encode(embedder: Any, texts: List[str]) -> Optional[List[Sequence[float]]]:
    """Vectors from an ``encode``-style embedder, or None if it is not one."""
    encode = None
    if callable(getattr(embedder, "encode", None)):
        encode = embedder.encode
    elif callable(embedder):
        encode = embedder
    if encode is None:
        return None
    vectors = encode(texts)
    if vectors is None:
        return None
    out = [list(vector) for vector in vectors]
    return out if len(out) == len(texts) else None


def _semantic_ranking(embedder: Any, query: str, rows: List[Dict[str, Any]],
                      k: int) -> Tuple[List[str], Dict[str, float], bool]:
    """``(ranked ids, scores, available)`` from a REAL embedder.

    Two shapes are accepted, because the app has both:

    * ``encode(list[str]) -> list[vector]`` — the fastembed / lane client that
      ``src.tool_index.ToolIndex._embed`` uses (or a bare callable); and
    * ``search(query, k) -> [{"memory_id"|"id", "score"}]`` — a vector store
      such as :class:`src.memory_vector.MemoryVectorStore`.

    ``available`` is False when the embedder is missing, is neither shape, or
    raised. A sick embedder is a degradation, never an error.
    """
    if embedder is None:
        return [], {}, False
    try:
        texts = [row["text"][:MAX_DOC_CHARS] for row in rows]
        vectors = _encode(embedder, [query] + texts)
        if vectors is not None:
            query_vector = vectors[0]
            scores: Dict[str, float] = {}
            for row, vector in zip(rows, vectors[1:]):
                score = hash_embed.similarity(query_vector, vector)
                if score > 0.0:
                    scores[row["id"]] = score
            return _ordered(scores), scores, True
        if callable(getattr(embedder, "search", None)):
            wanted = {row["id"] for row in rows}
            scores = {}
            for hit in embedder.search(query, k) or []:
                if not isinstance(hit, dict):
                    continue
                doc_id = str(hit.get("memory_id") or hit.get("id") or "")
                if doc_id not in wanted:
                    continue
                try:
                    value = float(hit.get("score") or 0.0)
                except (TypeError, ValueError):
                    value = 0.0
                if value > 0.0:
                    scores[doc_id] = value
            return _ordered(scores), scores, True
    except Exception as exc:  # noqa: BLE001 - the whole point of two tiers
        logger.debug("two-tier search: the refined lane raised (%s); staying on tier 1", exc)
        return [], {}, False
    logger.debug("two-tier search: %r is neither an encoder nor a store; staying on tier 1",
                 type(embedder).__name__)
    return [], {}, False


# ---------------------------------------------------------------------------
# Tier 3: a cross-encoder over what fusion produced
# ---------------------------------------------------------------------------


def _rerank_stage(reranker: Any, query: str, ordered: List[str],
                  originals: Dict[str, Dict[str, Any]],
                  head: int) -> Tuple[List[str], Dict[str, float], Optional[str]]:
    """``(order, scores, reason)``. ``reason`` is None only when it really ran.

    Reranking is a *reordering* of the fused head, never a filter: the tail
    past ``head`` keeps its fused position and follows, so turning the stage
    on can move a document up but can never make one vanish.

    ``reranker`` is either ``True`` (use the configured cross-encoder in
    :mod:`src.rerank`, imported lazily so this module keeps costing nothing to
    import) or a callable with that function's signature — which is how the
    tests, and any caller with its own scorer, inject one.
    """
    call = None
    if reranker is True:
        try:
            from src.rerank import rerank as call
        except Exception as exc:  # noqa: BLE001
            logger.debug("two-tier search: no rerank module (%s); staying on the fused order", exc)
            return ordered, {}, "no_reranker_configured"
    elif callable(reranker):
        call = reranker
    if call is None:
        return ordered, {}, "no_reranker_configured"

    head_ids = ordered[:max(1, int(head or RERANK_HEAD))]
    passages = [originals[doc_id] for doc_id in head_ids if doc_id in originals]
    if not passages:
        return ordered, {}, "no_reranker_configured"

    try:
        result = call(query, passages)
    except Exception as exc:  # noqa: BLE001 - an injected scorer is caller code
        # src.rerank never raises; a custom one might, and a search that 500s
        # because its optional third tier threw is exactly what this module
        # exists to prevent.
        logger.info("two-tier search: the rerank stage raised (%s); serving the fused order", exc)
        return ordered, {}, "endpoint_unreachable"

    if not getattr(result, "reranked", False):
        return ordered, {}, str(getattr(result, "reason", None) or "bad_response")

    indices = list(getattr(result, "order", None) or [])
    if indices and all(isinstance(i, int) and 0 <= i < len(passages) for i in indices):
        reordered = [str(passages[i].get("id")) for i in indices]
    else:
        # A scorer that returns rows rather than indices still has to be
        # honoured; fall back to reading the ids off what it handed back.
        reordered = [str(row.get("id")) for row in (getattr(result, "passages", None) or [])
                     if isinstance(row, dict)]
    reordered = [doc_id for doc_id in reordered if doc_id in originals]
    if not reordered:
        return ordered, {}, "bad_response"

    scores: Dict[str, float] = {}
    for doc_id, value in zip(reordered, list(getattr(result, "scores", None) or [])):
        try:
            scores[doc_id] = float(value)
        except (TypeError, ValueError):
            continue

    seen = set(reordered)
    tail = [doc_id for doc_id in ordered if doc_id not in seen]
    return reordered + tail, scores, None


# ---------------------------------------------------------------------------
# Corpus normalisation
# ---------------------------------------------------------------------------


def _rows(corpus: Iterable[Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """``([{"id", "text"}], {id: original row})`` over whatever came in.

    Junk is dropped, not raised on: a corpus assembled from a parsed export is
    exactly where a None or a stray string turns up.
    """
    rows: List[Dict[str, Any]] = []
    originals: Dict[str, Dict[str, Any]] = {}
    for entry in corpus or ():
        if not isinstance(entry, dict):
            continue
        doc_id = entry.get("id")
        if doc_id is None or doc_id == "":
            continue
        doc_id = str(doc_id)
        if doc_id in originals:
            continue
        text = entry.get("text")
        rows.append({"id": doc_id, "text": "" if text is None else str(text)})
        originals[doc_id] = entry
    return rows, originals


def _hit(original: Dict[str, Any], score: float, rank_index: int, tier: str) -> Dict[str, Any]:
    """One result: the caller's own row, plus where it landed and why."""
    hit = dict(original)
    hit["id"] = str(original.get("id"))
    hit["text"] = "" if original.get("text") is None else str(original.get("text"))
    hit["score"] = round(float(score), 6)
    hit["rank"] = rank_index
    hit["tier"] = tier
    return hit


def _result(hits: List[Dict[str, Any]], tier: str, degraded: bool,
            lanes: List[str], started: float,
            clock: Callable[[], float],
            rerank_requested: bool = False,
            rerank_reason: Optional[str] = None) -> Dict[str, Any]:
    try:
        elapsed = max(0.0, (clock() - started) * 1000.0)
    except Exception:  # noqa: BLE001 - an injected clock is caller code
        elapsed = 0.0
    out = {"hits": hits, "tier": tier, "degraded": degraded,
           "elapsed_ms": round(elapsed, 3), "lanes": lanes}
    # The key exists only for a caller that asked for reranking. Every
    # existing caller passes no reranker and must get back the dict it always
    # got, key for key — that is the compatibility guarantee.
    if rerank_requested:
        out["rerank_reason"] = rerank_reason
    return out


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def search(corpus: Iterable[Any], query: Any, k: int = DEFAULT_K, *,
           embedder: Any = None, reranker: Any = None,
           rerank_head: int = RERANK_HEAD,
           clock: Callable[[], float] = time.perf_counter) -> Dict[str, Any]:
    """Rank ``corpus`` against ``query``. Never raises.

    ``corpus`` is any iterable of ``{"id", "text", **meta}``; the meta travels
    through to the hit untouched, so a caller can attach a title, a source or
    an offset and get it back.

    Returns ``{"hits", "tier", "degraded", "elapsed_ms", "lanes"}`` where
    ``lanes`` names what actually contributed (``bm25``, ``hash``,
    ``embedder``, ``rerank``) — the evidence behind ``tier``.

    ``reranker`` opts into tier 3: ``True`` uses whatever cross-encoder
    :mod:`src.rerank` can resolve, or pass a callable with that module's
    ``rerank(query, passages)`` signature. When it is given the answer gains
    ``rerank_reason``, which is ``None`` when nothing was withheld — the stage
    ran, or there was nothing to rank — and otherwise names why the order you
    are holding is the fused one. ``tier`` says ``reranked`` only when it
    genuinely was.
    """
    # The injected clock is caller code, so even the first tick is guarded:
    # "never raises" has to include the stopwatch.
    try:
        started = clock() if callable(clock) else time.perf_counter()
    except Exception:  # noqa: BLE001
        started = 0.0
    try:
        return _search(corpus, query, k, embedder, reranker, rerank_head, started, clock)
    except Exception as exc:  # noqa: BLE001 - "never an error" is the contract
        logger.warning("two-tier search failed entirely (%s); answering empty", exc)
        return _result([], TIER_LEXICAL, True, [], started, clock,
                       rerank_requested=reranker is not None)


def _search(corpus: Iterable[Any], query: Any, k: int, embedder: Any,
            reranker: Any, rerank_head: int,
            started: float, clock: Callable[[], float]) -> Dict[str, Any]:
    try:
        k = max(1, min(int(k or DEFAULT_K), MAX_K))
    except (TypeError, ValueError):
        k = DEFAULT_K

    rows, originals = _rows(corpus)
    text = str(query if query is not None else "").strip()
    if not rows or not text:
        # Nothing to rank, so nothing to degrade: do not wake an embedder to
        # report an empty corpus, and do not claim a degradation that has no
        # consequence. The same reasoning covers the reranker, which is why
        # the reason here is None rather than a complaint.
        return _result([], TIER_LEXICAL, False, [], started, clock,
                       rerank_requested=reranker is not None)

    # ── tier 1, lane A: BM25-lite ──────────────────────────────────────────
    lexical = bm25_scores(text, [(row["id"], row["text"]) for row in rows])
    lexical_ranked = _ordered(lexical)
    lanes = ["bm25"]

    # ── tier 1, lane B: hash vectors (no model, no network) ────────────────
    hash_scores: Dict[str, float] = {}
    query_vector = hash_embed.embed(text)
    if any(query_vector):
        for row in rows:
            score = hash_embed.similarity(query_vector,
                                          hash_embed.embed(row["text"][:MAX_DOC_CHARS]))
            if score > 0.0:
                hash_scores[row["id"]] = score
    hash_ranked = _ordered(hash_scores)

    if hash_ranked:
        lanes.append("hash")
        tier = TIER_HYBRID
        tier1 = rrf(lexical_ranked, hash_ranked,
                    weights=(BM25_WEIGHT, HASH_WEIGHT))
    else:
        # Nothing could be vectorised at all — a query of pure punctuation, or
        # a corpus with no tokens. BM25 alone, and say so.
        tier = TIER_LEXICAL
        tier1 = dict(lexical)

    scores = tier1
    degraded = True

    # ── tier 2: the real embedder, mixed OVER tier 1 at α = 0.7 ────────────
    if embedder is not None:
        refined_ranked, refined_scores, available = _semantic_ranking(
            embedder, text, rows, max(k * 4, 20))
        if available and refined_ranked:
            lanes.append("embedder")
            tier1_norm = _normalised(tier1)
            refined_norm = _normalised(refined_scores)
            mixed: Dict[str, float] = {}
            for doc_id in set(tier1_norm) | set(refined_norm):
                mixed[doc_id] = (ALPHA * refined_norm.get(doc_id, 0.0)
                                 + (1.0 - ALPHA) * tier1_norm.get(doc_id, 0.0))
            scores = {doc_id: value for doc_id, value in mixed.items() if value > 0.0}
            tier = TIER_REFINED
            degraded = False

    ordered = _ordered(scores)

    # ── tier 3: a cross-encoder over the fused head, opt-in ────────────────
    rerank_reason: Optional[str] = None
    if reranker is not None:
        reranked_order, rerank_scores, rerank_reason = _rerank_stage(
            reranker, text, ordered, originals, rerank_head)
        if rerank_reason is None:
            lanes.append("rerank")
            ordered = reranked_order
            tier = TIER_RERANKED
            # The reranker's own score is what produced this order, so it is
            # what the hit reports; a fused score left in place would let a
            # caller that re-sorts by score silently undo the reranking. It is
            # on the cross-encoder's scale, which may be negative logits.
            scores = {doc_id: rerank_scores.get(doc_id, scores.get(doc_id, 0.0))
                      for doc_id in ordered}

    hits = [_hit(originals[doc_id], scores.get(doc_id, 0.0), index, tier)
            for index, doc_id in enumerate(ordered[:k], start=1)
            if doc_id in originals]
    return _result(hits, tier, degraded, lanes, started, clock,
                   rerank_requested=reranker is not None,
                   rerank_reason=rerank_reason)


# ---------------------------------------------------------------------------
# Presentation: the piece of the document that matched, and where it is
# ---------------------------------------------------------------------------


def snippet(text: Any, query: Any, width: int = SNIPPET_CHARS) -> Dict[str, Any]:
    """``{"text", "start", "end", "match_start", "match_end"}``.

    The window around the first query term that actually occurs in ``text``,
    with the offsets **into the original string** so a caller can highlight
    the real span instead of re-searching the excerpt. When no term occurs the
    window is the head of the text and ``match_start`` is ``None`` — a
    highlight is never invented for a match that is not there.
    """
    body = "" if text is None else str(text)
    try:
        width = max(40, min(int(width), 4000))
    except (TypeError, ValueError):
        width = SNIPPET_CHARS
    lowered = body.lower()
    match_start: Optional[int] = None
    match_end: Optional[int] = None
    # Longest term first: matching "objectives" beats matching "the".
    for term in sorted(set(hash_embed.tokens(query)), key=lambda t: (-len(t), t)):
        found = lowered.find(term)
        if found >= 0:
            match_start, match_end = found, found + len(term)
            break
    if match_start is None:
        start = 0
    else:
        start = max(0, match_start - width // 3)
    end = min(len(body), start + width)
    if start > 0:
        # Do not start mid-word when a space is close by.
        space = body.find(" ", start, min(start + 20, end))
        if space > start:
            start = space + 1
    return {"text": body[start:end], "start": start, "end": end,
            "match_start": match_start, "match_end": match_end}
