"""src/compare_elo.py — Elo ratings from model-comparison votes.

Every vote in the comparator (two or more models, one prompt, a winner or a
tie) is a set of pairwise results: the winner beat each other model, or, on a
tie, every pair drew. Ratings start at 1000 and move with K = 32, in vote
order, using the ratings as they stood BEFORE the vote for every pair of that
vote (so the order of models inside one vote does not matter).

Two tables are kept: an overall one, and one per topic, where the topic is a
label derived from the prompt (`src/prompt_topics.py`). Nothing is stored
beyond the votes themselves: ratings are recomputed from them, so a better
topic classifier or a deleted comparison is reflected immediately.

`router_ratings` is the read-only hook `src/model_router.py` uses when its
`use_elo` setting is on.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src import prompt_topics

logger = logging.getLogger(__name__)

K = 32
START = 1000.0
MIN_GAMES_FOR_ROUTING = 5


def expected(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def _new_row(label: str) -> Dict[str, Any]:
    return {"label": label, "rating": START, "games": 0, "wins": 0, "losses": 0, "ties": 0}


def apply_vote(table: Dict[str, Dict[str, Any]], keys: Sequence[str], labels: Mapping[str, str],
               winner: Optional[str]) -> None:
    """Fold one vote into `table` (key -> row). `winner` is a key from `keys`
    or ``"tie"``; anything else (a vote naming nobody) is ignored."""
    keys = list(dict.fromkeys(k for k in keys if k))
    if len(keys) < 2:
        return
    if winner != "tie" and winner not in keys:
        return
    for k in keys:
        table.setdefault(k, _new_row(labels.get(k, k)))
    before = {k: table[k]["rating"] for k in keys}
    delta = {k: 0.0 for k in keys}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if winner == "tie":
                sa = 0.5
            else:
                sa = 1.0 if winner == a else 0.0 if winner == b else None
                if sa is None:      # neither of this pair won: no information about them
                    continue
            ea = expected(before[a], before[b])
            delta[a] += K * (sa - ea)
            delta[b] += K * ((1.0 - sa) - (1.0 - ea))
    for k in keys:
        row = table[k]
        row["rating"] = round(row["rating"] + delta[k], 3)
        row["games"] += 1
        if winner == "tie":
            row["ties"] += 1
        elif winner == k:
            row["wins"] += 1
        else:
            row["losses"] += 1


def _rows_out(table: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = [{"key": k, **v, "rating": round(v["rating"], 1)} for k, v in table.items()]
    out.sort(key=lambda r: (-r["rating"], -r["games"], r["key"]))
    return out


def compute(votes: Iterable[Mapping[str, Any]], *, mode: Optional[str] = None) -> Dict[str, Any]:
    """`votes`: dicts with ``keys`` (rating keys, in pane order), ``labels``
    (key -> display label), ``winner`` (a key or ``"tie"``), ``prompt``,
    optional ``mode``; in chronological order."""
    overall: Dict[str, Dict[str, Any]] = {}
    topics: Dict[str, Dict[str, Dict[str, Any]]] = {}
    topic_votes: Dict[str, int] = {}
    used = 0
    for v in votes:
        if mode and v.get("mode") and v.get("mode") != mode:
            continue
        keys, labels, winner = v.get("keys") or [], v.get("labels") or {}, v.get("winner")
        if len(set(keys)) < 2 or (winner != "tie" and winner not in keys):
            continue
        used += 1
        apply_vote(overall, keys, labels, winner)
        topic = prompt_topics.classify(str(v.get("prompt") or ""))
        apply_vote(topics.setdefault(topic, {}), keys, labels, winner)
        topic_votes[topic] = topic_votes.get(topic, 0) + 1
    return {
        "k": K, "start": START, "votes": used,
        "overall": _rows_out(overall),
        "topics": {t: _rows_out(tab) for t, tab in sorted(topics.items())},
        "topic_votes": dict(sorted(topic_votes.items())),
    }


# ── stored comparisons -> votes ────────────────────────────────────────────

def votes_from_comparisons(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    """Normalise `Comparison` rows (oldest first) into `compute` votes.

    Two shapes are stored: comparisons run through ``/start`` + ``/vote``
    (winner "a" / "b" / "tie", models in model_a / model_b) and votes recorded
    by the Studio through ``/record`` (winner is a model label or "tie";
    ``blind_mapping`` may carry the full model list, the raw model ids and the
    mode). Rows without a winner are skipped."""
    votes: List[Dict[str, Any]] = []
    for c in rows:
        winner_raw = (getattr(c, "winner", None) or "").strip()
        if not winner_raw:
            continue
        extra: Dict[str, Any] = {}
        raw_map = getattr(c, "blind_mapping", None)
        if raw_map:
            try:
                parsed = json.loads(raw_map)
                extra = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                extra = {}
        labels_list = extra.get("models") if isinstance(extra.get("models"), list) else None
        if not labels_list:
            labels_list = [getattr(c, "model_a", ""), getattr(c, "model_b", "")]
        labels_list = [str(x) for x in labels_list]
        ids = extra.get("model_ids") if isinstance(extra.get("model_ids"), list) else None
        if ids and len(ids) == len(labels_list):
            keys = [str(i or l) for i, l in zip(ids, labels_list)]
        else:
            keys = list(labels_list)
        labels = dict(zip(keys, labels_list))
        if winner_raw == "tie":
            winner = "tie"
        elif winner_raw in ("a", "b") and len(keys) == 2 and not extra.get("models"):
            winner = keys[0] if winner_raw == "a" else keys[1]
        elif winner_raw in labels_list:
            winner = keys[labels_list.index(winner_raw)]
        elif winner_raw in keys:
            winner = winner_raw
        else:
            continue
        votes.append({"keys": keys, "labels": labels, "winner": winner,
                      "prompt": getattr(c, "prompt", "") or "", "mode": extra.get("mode")})
    return votes


def load_votes(owner: Optional[str]) -> List[Dict[str, Any]]:
    from core.database import Comparison, SessionLocal
    db = SessionLocal()
    try:
        q = db.query(Comparison).filter(Comparison.winner.isnot(None))
        if owner:
            q = q.filter(Comparison.owner == owner)
        rows = q.order_by(Comparison.voted_at, Comparison.created_at).all()
        return votes_from_comparisons(rows)
    finally:
        db.close()


# ── read-only hook for the router ──────────────────────────────────────────

def router_ratings(owner: Optional[str], topic: Optional[str] = None,
                   *, min_games: int = MIN_GAMES_FOR_ROUTING) -> Dict[str, Dict[str, Any]]:
    """``{model key: {"rating", "games", "scope"}}`` for models with at least
    `min_games` votes: the topic table when `topic` is given and the model has
    enough games there, otherwise the overall table. Empty on any failure:
    the router must never depend on this."""
    try:
        result = compute(load_votes(owner))
    except Exception as exc:  # noqa: BLE001
        logger.debug("compare_elo: ratings unavailable: %s", exc)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for row in result["overall"]:
        if row["games"] >= min_games:
            out[row["key"]] = {"rating": row["rating"], "games": row["games"], "scope": "overall"}
    if topic:
        for row in result["topics"].get(topic, []):
            if row["games"] >= min_games:
                out[row["key"]] = {"rating": row["rating"], "games": row["games"], "scope": f"topic:{topic}"}
    return out


__all__ = ["K", "START", "expected", "apply_vote", "compute", "votes_from_comparisons",
           "load_votes", "router_ratings"]
