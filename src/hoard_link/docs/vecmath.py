"""Vector math for embeddings: float32 BLOBs, cosine, top-k and rank fusion.

Standard library only; numpy is used when it is importable (``topk`` over a matrix) and never required. The Node
twin is ``js/hoard-commons/docs.js`` (``normalize``, ``cosine``, ``packVec``, ``unpackVec``, ``topk``, ``rrf``),
checked against ``tests/vectors/docs_vec.json``.

* :func:`pack_vec` / :func:`unpack_vec` — float32 little-endian, byte for byte what Borges stores in its
  ``embeddings.vector`` BLOB and Hypatia in ``array('f')`` BLOBs, so the databases are interchangeable.
* :func:`topk` — the ``k`` best rows of a matrix by cosine (numpy ``argpartition`` when available, a heap
  otherwise), which lifts Hypatia's old 6000-vector scan cap.
* :func:`rrf` (reciprocal rank fusion, Borges and Vitruvius ``k=60``) and :func:`minmax_fuse` (Hypatia).

The embedding *model* is a family service (``fam_embed``); this module is only the maths around it.
"""

from __future__ import annotations

import heapq
import math
import struct
import sys
from array import array
from typing import Any, Hashable, Iterable, Optional, Sequence

__all__ = ["normalize", "cosine", "dot", "pack_vec", "unpack_vec", "topk", "rrf", "minmax_fuse"]


def _floats(v: Any) -> list[float]:
    if hasattr(v, "tolist"):
        v = v.tolist()
    return [float(x) for x in v]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return math.fsum(x * y for x, y in zip(_floats(a), _floats(b)))


def normalize(v: Sequence[float]) -> list[float]:
    """The unit vector (L2); a zero vector is returned unchanged (as zeros)."""
    xs = _floats(v)
    norm = math.sqrt(math.fsum(x * x for x in xs))
    return [x / norm for x in xs] if norm > 0 else xs


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, ``0.0`` when either vector is zero or the lengths differ."""
    xs, ys = _floats(a), _floats(b)
    if len(xs) != len(ys) or not xs:
        return 0.0
    na = math.sqrt(math.fsum(x * x for x in xs))
    nb = math.sqrt(math.fsum(y * y for y in ys))
    if na == 0 or nb == 0:
        return 0.0
    return math.fsum(x * y for x, y in zip(xs, ys)) / (na * nb)


def pack_vec(v: Sequence[float]) -> bytes:
    """Float32 little-endian bytes (4 per component)."""
    if hasattr(v, "astype"):
        return v.astype("<f4").tobytes()
    arr = array("f", _floats(v))
    if sys.byteorder == "big":
        arr.byteswap()
    return arr.tobytes()


def unpack_vec(blob: bytes, dim: Optional[int] = None) -> list[float]:
    """The components of a float32 little-endian BLOB. ``ValueError`` if the length is not a multiple of 4 (or
    not ``4 * dim`` when ``dim`` is given)."""
    b = bytes(blob or b"")
    if len(b) % 4 or (dim is not None and len(b) != 4 * dim):
        raise ValueError(f"vector blob of {len(b)} bytes is not {dim if dim is not None else 'a whole number of'} float32 values")
    return list(struct.unpack(f"<{len(b) // 4}f", b))


def topk(matrix: Any, query: Sequence[float], k: int, min_score: float = 0.0, *, normalized: bool = False) -> list[tuple[int, float]]:
    """The ``k`` rows of ``matrix`` most similar to ``query``: ``[(row_index, score), …]``, best first, scores
    below ``min_score`` dropped. Score is cosine; pass ``normalized=True`` when every row and the query are
    already unit vectors to use the plain dot product (faster). Ties go to the lower row index (the numpy path
    may order exact ties at the cut-off differently)."""
    k = int(k)
    if k <= 0:
        return []
    try:
        import numpy as np  # type: ignore
    except ImportError:
        np = None
    if np is not None:
        m = np.asarray(matrix, dtype=np.float32)
        if m.size == 0:
            return []
        if m.ndim != 2:
            raise ValueError("matrix must be 2-dimensional")
        q = np.asarray(query, dtype=np.float32)
        if q.shape[0] != m.shape[1]:
            raise ValueError("query length does not match the matrix width")
        if normalized:
            scores = m @ q
        else:
            qn = float(np.linalg.norm(q))
            norms = np.linalg.norm(m, axis=1) * qn
            with np.errstate(divide="ignore", invalid="ignore"):
                scores = np.where(norms > 0, (m @ q) / norms, 0.0)
        n = scores.shape[0]
        if k < n:
            idx = np.argpartition(-scores, k - 1)[:k]
        else:
            idx = np.arange(n)
        order = sorted(((int(i), float(scores[i])) for i in idx if scores[i] >= min_score), key=lambda t: (-t[1], t[0]))
        return order[:k]

    q = _floats(query)
    qn = math.sqrt(math.fsum(x * x for x in q))

    def rows() -> Iterable[tuple[float, int]]:
        for i, row in enumerate(matrix):
            r = _floats(row)
            if len(r) != len(q):
                raise ValueError("row length does not match the query length")
            if normalized:
                s = math.fsum(x * y for x, y in zip(r, q))
            else:
                rn = math.sqrt(math.fsum(x * x for x in r))
                s = math.fsum(x * y for x, y in zip(r, q)) / (rn * qn) if rn > 0 and qn > 0 else 0.0
            if s >= min_score:
                yield (s, i)

    best = heapq.nlargest(k, rows(), key=lambda t: (t[0], -t[1]))
    return [(i, s) for s, i in best]


def rrf(rankings: Iterable[Sequence[Hashable]], weights: Optional[Sequence[float]] = None, k: int = 60) -> list[tuple[Hashable, float]]:
    """Reciprocal rank fusion: ``score(id) = Σ weight / (k + rank)`` over the rankings (ranks start at 1), best
    first; ties keep first-seen order."""
    fused: dict[Hashable, float] = {}
    for i, ranking in enumerate(rankings):
        w = float(weights[i]) if weights is not None and i < len(weights) else 1.0
        for rank, item in enumerate(ranking, start=1):
            fused[item] = fused.get(item, 0.0) + w / (k + rank)
    order = {item: n for n, item in enumerate(fused)}
    return sorted(fused.items(), key=lambda kv: (-kv[1], order[kv[0]]))


def minmax_fuse(scored: Iterable[Sequence[tuple[Hashable, float]]], weights: Optional[Sequence[float]] = None) -> list[tuple[Hashable, float]]:
    """Fuse score lists ``[(id, raw_score), …]``: each list is min-max scaled to 0..1 (a list whose scores are all
    equal counts 1.0), then summed with ``weights`` (default: the mean of the lists). Best first."""
    lists = [list(s) for s in scored]
    if not lists:
        return []
    ws = [float(w) for w in weights] if weights is not None else [1.0 / len(lists)] * len(lists)
    fused: dict[Hashable, float] = {}
    for i, items in enumerate(lists):
        if not items:
            continue
        lo = min(s for _, s in items)
        hi = max(s for _, s in items)
        w = ws[i] if i < len(ws) else 0.0
        for item, s in items:
            scaled = 1.0 if hi - lo < 1e-12 else (s - lo) / (hi - lo)
            fused[item] = fused.get(item, 0.0) + w * scaled
    order = {item: n for n, item in enumerate(fused)}
    return sorted(fused.items(), key=lambda kv: (-kv[1], order[kv[0]]))
