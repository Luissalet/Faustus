"""convergence.py — has an iterative fix loop stopped producing change?

A fixed round counter ("run exactly N fix rounds") spends model time on rounds
that change nothing and stops short of the round that would have mattered. The
honest question is not "how many rounds have I run" but "are the rounds still
producing change". This module answers that from the ARTIFACTS of the rounds
themselves — the diff each round produced, or the verification output it left
behind — with no model call and no I/O:

    score = 0.35*size_trend + 0.35*change_velocity + 0.30*similarity_trend

* ``size_trend``       1.0 when successive artifacts stop changing LENGTH
                       (mean absolute relative delta of the last pairs, inverted).
* ``change_velocity``  1.0 when the distance between successive artifacts
                       approaches 0 (a token-set Jaccard distance, inverted).
* ``similarity_trend`` 1.0 when successive similarity is high AND rising — two
                       rounds that are both different but *increasingly*
                       similar are converging; two that oscillate are not.

``assess(rounds)`` returns the score, the components, a confidence band and a
one-line reason. Fewer than two rounds is always ``early``: one artifact has no
trend to read, and calling that "converged" would stop a loop before it began.

Pure and stdlib-only by design (it runs inside `src/dispatch.py`'s fix loop):
every entry point is total — bad input yields the "early" verdict, never an
exception.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Set

# The weights of the three components (they sum to 1.0).
W_SIZE = 0.35
W_VELOCITY = 0.35
W_SIMILARITY = 0.30

# Score bands. `converged` is the top one: the loop may stop here.
CONVERGED_SCORE = 0.75
MODERATE_SCORE = 0.50

# How many successive pairs of rounds the trend is read from (the newest ones).
TREND_PAIRS = 3
# A trend needs at least two artifacts to compare.
MIN_ROUNDS = 2

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: Any) -> Set[str]:
    """Lower-cased word/identifier tokens of `text` (never raises)."""
    try:
        return set(_TOKEN_RE.findall(str(text or "").lower()))
    except Exception:  # noqa: BLE001 - a scoring helper never raises
        return set()


def similarity(a: Any, b: Any) -> float:
    """Jaccard similarity of two texts' token sets, in [0, 1].

    Two empty artifacts are identical (1.0) — a fix round that produced no
    output twice in a row HAS converged; one empty and one not is 0.0.
    """
    ta, tb = tokenize(a), tokenize(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    union = len(ta | tb)
    return (len(ta & tb) / union) if union else 1.0


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if value != value:                      # NaN
        return lo
    return lo if value < lo else (hi if value > hi else value)


def _mean(values: Sequence[float]) -> float:
    return (sum(values) / len(values)) if values else 0.0


def _size_trend(pairs: Sequence[tuple]) -> float:
    """1.0 when successive lengths stop changing."""
    deltas: List[float] = []
    for prev, cur in pairs:
        a, b = len(prev), len(cur)
        base = max(a, b, 1)
        deltas.append(_clamp(abs(b - a) / base))
    return _clamp(1.0 - _mean(deltas))


def _velocity(sims: Sequence[float]) -> float:
    """1.0 when the edit distance between successive rounds approaches 0."""
    return _clamp(1.0 - _mean([1.0 - s for s in sims]))


def _similarity_trend(sims: Sequence[float]) -> float:
    """High AND rising similarity. With a single pair there is no slope to
    read, so the level itself is the whole verdict."""
    level = _clamp(_mean(sims))
    if len(sims) < 2:
        return level
    slope = sims[-1] - sims[0]
    rising = _clamp(0.5 + slope / 2.0)
    return _clamp(0.5 * level + 0.5 * rising)


def _confidence(score: float) -> str:
    if score >= CONVERGED_SCORE:
        return "high"
    if score >= MODERATE_SCORE:
        return "moderate"
    return "early"


def _early(n: int, reason: str) -> Dict[str, Any]:
    return {
        "score": 0.0,
        "confidence": "early",
        "converged": False,
        "components": {"size_trend": 0.0, "change_velocity": 0.0, "similarity_trend": 0.0},
        "rounds": n,
        "reason": reason,
    }


def assess(rounds: Any) -> Dict[str, Any]:
    """Read the trend of an ordered list of round artifacts (strings: the diff
    or the verification output of each round).

    Returns ``{"score", "confidence", "converged", "components", "rounds",
    "reason"}``. ``converged`` is True only in the ``high`` band — that is the
    signal a caller may act on to stop its loop early.
    """
    try:
        items = [str(r if r is not None else "") for r in (rounds or [])]
    except TypeError:                        # not iterable
        return _early(0, "no rounds to assess")
    n = len(items)
    if n < MIN_ROUNDS:
        return _early(n, f"{n} round(s) so far — at least {MIN_ROUNDS} are needed to see a trend")
    pairs = [(items[i - 1], items[i]) for i in range(1, n)][-TREND_PAIRS:]
    sims = [similarity(a, b) for a, b in pairs]
    size = _size_trend(pairs)
    velocity = _velocity(sims)
    trend = _similarity_trend(sims)
    score = _clamp(W_SIZE * size + W_VELOCITY * velocity + W_SIMILARITY * trend)
    confidence = _confidence(score)
    converged = confidence == "high"
    if converged:
        reason = (f"the last {len(pairs)} round(s) changed almost nothing "
                  f"(size {size:.2f}, change {velocity:.2f}, trend {trend:.2f}) — further rounds are unlikely to")
    elif confidence == "moderate":
        reason = f"rounds are still changing things ({len(pairs)} pair(s) compared, score {score:.2f})"
    else:
        reason = f"rounds are still producing very different results (score {score:.2f})"
    return {
        "score": round(score, 3),
        "confidence": confidence,
        "converged": converged,
        "components": {
            "size_trend": round(size, 3),
            "change_velocity": round(velocity, 3),
            "similarity_trend": round(trend, 3),
        },
        "rounds": n,
        "reason": reason,
    }
