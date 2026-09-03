"""Embeddings with no model and no network — the fallback that is never absent.

A freshly installed Faustus has downloaded nothing: no fastembed model, no
ChromaDB container, no embedding endpoint. Every semantic lane in this app is
allowed to be missing, and each one degrades to "lexical only". That is
correct, but it means the first hour with the app is the worst hour, which is
exactly backwards.

This module is the floor under all of it: a **hashing embedder** that needs no
model, no download and no network, and that produces the same vector on every
machine and in every process.

    tokenize → FNV-1a 32-bit per token → bucket into `dims` → weight by a
    sublinear term frequency (1 + log tf) → L2-normalise

Three properties are load-bearing and each has a test:

* **Deterministic across processes.** The hash is FNV-1a over the token's
  UTF-8 bytes, computed here. Python's built-in ``hash()`` is salted per
  process (``PYTHONHASHSEED``), so a vector written by one process would not
  match one computed by the next — a cache or an index built with it would be
  silently wrong. It is never used.
* **A zero vector is a legal answer.** An empty string, or a string with no
  tokens, embeds to all zeros and :func:`similarity` answers 0.0 for it. There
  is no division by zero anywhere in this file.
* **It is deliberately weaker than a real embedder** and says so. It has no
  idea that "car" and "automobile" are related; what it does have is
  robustness to the words a user actually repeats, and a bounded, explainable
  cost. Its job is to never leave the user with nothing while the good lane is
  missing, not to replace it. :mod:`src.two_tier_search` mixes the real
  embedder over it at α = 0.7 the moment one exists.

Tokenizer choice — ``src.memory.tokenize``, lowercased
-----------------------------------------------------
The two candidates already in the tree are ``src.memory.tokenize`` (splits on
whitespace, strips ``.,!?";``, returns a **list**) and
``src.personal_docs.tokenize`` (a ``[A-Za-z0-9_-]+`` regex, lowercases, drops
stop words and 1-character tokens, returns a **set**).

This module uses ``src.memory.tokenize``, lowercased, for two reasons:

1. It returns a list, so it keeps **term frequency**. The weighting the report
   specifies is ``1 + log(tf)``; over ``personal_docs.tokenize``'s set every tf
   is 1, the weight collapses to the constant 1.0, and the sublinear term is
   dead code.
2. It is the same normalisation ``src.memory_engine.bm25_scores`` already
   applies (``_tokens`` there is ``tokenize()`` lowercased), so the lexical
   lane and this lane rank over **one vocabulary**. Fusing two rankings built
   from different tokenizers by RRF fuses two different questions.

The cost is that stop words get buckets of their own. That is tolerable here
precisely because this lane is never alone: BM25's IDF discounts them in the
other tier-1 lane, and RRF fuses *ranks*, so the discriminating lane carries
the result.

Pure stdlib. numpy is a hard dependency of the app (requirements.txt) but is
deliberately not used: this is the lane that has to work when everything else
is missing, and a corpus of one user's own messages is small enough that pure
Python is not the bottleneck.
"""

from __future__ import annotations

import logging
import math
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.memory import tokenize as _tokenize

logger = logging.getLogger(__name__)

__all__ = [
    "DIMS",
    "fnv1a_32",
    "tokens",
    "term_frequencies",
    "embed",
    "embed_many",
    "similarity",
    "rank",
]

# The report's dimensionality. 384 is also fastembed's all-MiniLM width, so a
# hash vector and a real one are at least commensurable in size.
DIMS = 384

# FNV-1a, 32-bit. RFC-less but universally specified; these three constants
# are the whole algorithm and must never change — a different offset basis
# means every vector ever cached with the old one is wrong.
_FNV_OFFSET_32 = 0x811C9DC5
_FNV_PRIME_32 = 0x01000193
_MASK_32 = 0xFFFFFFFF


def fnv1a_32(text: Any) -> int:
    """FNV-1a 32-bit over the UTF-8 bytes of ``text``.

    Deterministic across processes, machines and Python versions — which is
    the entire reason this exists instead of ``hash()``.
    """
    data = str(text if text is not None else "").encode("utf-8", "surrogatepass")
    digest = _FNV_OFFSET_32
    for byte in data:
        digest ^= byte
        digest = (digest * _FNV_PRIME_32) & _MASK_32
    return digest


@lru_cache(maxsize=100_000)
def _bucket(token: str, dims: int) -> int:
    """Which of ``dims`` buckets a token lands in. Cached: a corpus repeats
    its vocabulary constantly, and the hash is the only real cost here."""
    return fnv1a_32(token) % dims


def tokens(text: Any) -> List[str]:
    """``src.memory.tokenize`` lowercased, with the empties dropped.

    A list, not a set: the caller needs term frequency.
    """
    try:
        raw = _tokenize(str(text if text is not None else ""))
    except Exception:  # noqa: BLE001 - a tokenizer must never cost the lane
        logger.debug("hash_embed: tokenizer raised; treating the text as empty")
        return []
    out: List[str] = []
    for token in raw:
        token = str(token or "").strip().lower()
        if token:
            out.append(token)
    return out


def term_frequencies(text: Any) -> Dict[str, int]:
    """``{token: count}`` — the input to the sublinear weighting."""
    counts: Dict[str, int] = {}
    for token in tokens(text):
        counts[token] = counts.get(token, 0) + 1
    return counts


def _clean_dims(dims: Any) -> int:
    try:
        value = int(dims)
    except (TypeError, ValueError):
        return DIMS
    # One bucket is a legal (useless) vector; a non-positive one is not.
    return value if value >= 1 else DIMS


def embed(text: Any, dims: int = DIMS) -> List[float]:
    """The hashed, sublinearly weighted, L2-normalised vector for ``text``.

    An empty string — or a string with no tokens at all — returns a vector of
    zeros. That is a legal answer, not an error: :func:`similarity` scores it
    0.0 against everything and never divides by zero.
    """
    width = _clean_dims(dims)
    vector = [0.0] * width
    counts = term_frequencies(text)
    if not counts:
        return vector
    for token, freq in counts.items():
        # 1 + log(tf): the fifth occurrence of a word says much less than the
        # second. log() of a positive int is always defined here.
        vector[_bucket(token, width)] += 1.0 + math.log(freq)
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:                      # pragma: no cover - counts were non-empty
        return [0.0] * width
    return [value / norm for value in vector]


def embed_many(texts: Iterable[Any], dims: int = DIMS) -> List[List[float]]:
    """:func:`embed` over a sequence, in order."""
    return [embed(text, dims) for text in (texts or [])]


def similarity(a: Optional[Sequence[float]], b: Optional[Sequence[float]]) -> float:
    """Cosine similarity, clamped to [-1, 1].

    Defensive on every axis a caller can get wrong: a missing vector, vectors
    of different widths, a zero vector, or a non-numeric element all answer
    0.0 rather than raising. Vectors from :func:`embed` are already
    normalised, so this is a dot product; it renormalises anyway so a caller
    may pass raw vectors.
    """
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        # Two different embedders' vectors are not comparable. Saying "0.0" is
        # the honest answer; raising would take down a search.
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    try:
        for left, right in zip(a, b):
            left = float(left)
            right = float(right)
            dot += left * right
            norm_a += left * left
            norm_b += right * right
    except (TypeError, ValueError):
        return 0.0
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    score = dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
    return max(-1.0, min(1.0, score))


def rank(query: Any, docs: Sequence[Tuple[str, Any]], dims: int = DIMS,
         ) -> List[Tuple[str, float]]:
    """``[(doc_id, similarity)]`` best first, dropping non-positive scores.

    Ties break on the id so the order is total and reproducible — a ranking
    that reshuffles on equal scores makes RRF non-deterministic.
    """
    query_vector = embed(query, dims)
    if not any(query_vector):
        return []
    scored: List[Tuple[str, float]] = []
    for doc_id, text in docs or []:
        score = similarity(query_vector, embed(text, dims))
        if score > 0.0:
            scored.append((str(doc_id), score))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored
