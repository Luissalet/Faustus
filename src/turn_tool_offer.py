"""turn_tool_offer.py - which tools a turn really offered the model, and what they weighed (OBJ-56, #327).

`src/tool_footprint.py` measures the whole catalogue: an upper bound, as if every tool went out at once. Per-turn tool
selection sends far fewer, and which ones depends on the message. This module keeps the real list:

* the agent loop calls ``record(session_id, round, schemas, model)`` right before each model call with the exact tool
  list it sends (round 1 starts a new turn for that session);
* ``save_assistant_response`` calls ``take(session_id)`` and stores the summary in the turn's metadata as
  ``tools_offered``: distinct tools offered over the turn and their tokens (same estimator and function-call shape as
  the catalogue footprint, so the numbers add up the same way), totals per source, every tool with its source and
  tokens (``tools``), the heaviest ones in detail (``heaviest``, the UI summary), and per round how many tools went out
  and what they cost (``sent_tokens`` is the sum over rounds: what the turn really paid);
* ``report(...)`` reads those records back from saved turns (the harness MCP tool ``turn_tools``).

Best effort everywhere: a failure here never costs a turn.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Dict, Iterable, List, Mapping, Optional

from src import tool_footprint as tf

#: Heaviest tools detailed in a turn's record (every tool is listed in ``tools`` anyway).
TOP = 10
#: Turns kept in memory between their first round and their save. Every round refreshes its turn, so only the turns
#: idle the longest (abandoned before a save) are dropped when there are more.
_MAX_SESSIONS = 1024

_LOCK = threading.Lock()
_TURNS: Dict[str, Dict[str, Any]] = {}


def _cost_key(row: Mapping[str, Any]) -> str:
    raw = json.dumps([row.get("name"), row.get("description"), row.get("input_schema")], sort_keys=True,
                     ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def costs_of(schemas: Iterable[Any], model: str = "", cache: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """One cost row per tool (name, source, tokens, description_tokens, schema_tokens), measured like the catalogue."""
    rows = tf.rows_from_mappings(list(schemas or []))
    count = tf.default_counter(model)
    out: List[Dict[str, Any]] = []
    for row in rows:
        key = _cost_key(row)
        hit = cache.get(key) if cache is not None else None
        if hit is None:
            cost = tf.tool_costs([row], count=count)[0]
            hit = {k: v for k, v in cost.items() if not k.startswith("_") and k not in ("hidden", "exposure")}
            if cache is not None:
                cache[key] = hit
        out.append(hit)
    return out


def summarize(costs: Iterable[Mapping[str, Any]], *, top: int = TOP) -> Dict[str, Any]:
    """Count, tokens, per-source totals and the heaviest tools of a set of cost rows (one row per distinct tool)."""
    unique: Dict[str, Mapping[str, Any]] = {}
    for c in costs:
        unique[str(c.get("name"))] = c
    rows = sorted(unique.values(), key=lambda c: (-int(c.get("tokens") or 0), str(c.get("name"))))
    by_source: Dict[str, Dict[str, int]] = {}
    for c in rows:
        src = by_source.setdefault(str(c.get("source") or "builtin"), {"count": 0, "tokens": 0})
        src["count"] += 1
        src["tokens"] += int(c.get("tokens") or 0)
    return {
        "count": len(rows),
        "tokens": sum(int(c.get("tokens") or 0) for c in rows),
        "by_source": dict(sorted(by_source.items(), key=lambda kv: -kv[1]["tokens"])),
        "tools": [{"name": c.get("name"), "source": c.get("source"), "tokens": c.get("tokens")} for c in rows],
        "heaviest": [{"name": c.get("name"), "source": c.get("source"), "tokens": c.get("tokens"),
                      "description_tokens": c.get("description_tokens"), "schema_tokens": c.get("schema_tokens")}
                     for c in rows[:max(0, int(top))]],
    }


def record(session_id: Any, round_num: Any, schemas: Optional[Iterable[Any]], model: str = "") -> None:
    """Remember the tool list one model call of this turn sends. Round 1 (or the first call seen) starts the turn."""
    try:
        sid = str(session_id or "").strip()
        if not sid:
            return
        try:
            rnd = int(round_num or 1)
        except (TypeError, ValueError):
            rnd = 1
        with _LOCK:
            turn = _TURNS.pop(sid, None)                  # re-inserted below: the dict stays in recency order
            if turn is None or rnd <= 1:
                turn = {"model": model or "", "rounds": [], "tools": {}, "cache": {}}
            _TURNS[sid] = turn
            while len(_TURNS) > _MAX_SESSIONS:
                _TURNS.pop(next(iter(_TURNS)))
            costs = costs_of(schemas or [], model or turn["model"], turn["cache"])
            for c in costs:
                turn["tools"][str(c["name"])] = c
            turn["rounds"].append({"round": rnd, "count": len(costs), "tokens": sum(int(c["tokens"]) for c in costs)})
    except Exception:  # noqa: BLE001 - never cost a round
        return


def take(session_id: Any) -> Optional[Dict[str, Any]]:
    """The turn's ``tools_offered`` record (and forget it), or None when no model call was recorded."""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    with _LOCK:
        turn = _TURNS.pop(sid, None)
    if not turn or not turn["rounds"]:
        return None
    out = summarize(turn["tools"].values())
    out["rounds"] = turn["rounds"]
    out["sent_tokens"] = sum(r["tokens"] for r in turn["rounds"])
    if turn.get("model"):
        out["model"] = turn["model"]
    return out


def peek(session_id: Any) -> Optional[Dict[str, Any]]:
    """Same as ``take`` without forgetting (for a turn still running)."""
    sid = str(session_id or "").strip()
    with _LOCK:
        turn = _TURNS.get(sid)
        if not turn or not turn["rounds"]:
            return None
        tools, rounds, model = list(turn["tools"].values()), list(turn["rounds"]), turn.get("model")
    out = summarize(tools)
    out.update({"rounds": rounds, "sent_tokens": sum(r["tokens"] for r in rounds), "running": True})
    if model:
        out["model"] = model
    return out


def _metadata(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def report(session_id: Optional[str] = None, *, message_id: Optional[str] = None, limit: int = 5,
           owner: Optional[str] = None, db=None) -> Dict[str, Any]:
    """The ``tools_offered`` records of saved turns, newest first: one message, the last turns of a session, or the last
    turns across chats (optionally of one owner)."""
    from core.database import ChatMessage, Session, SessionLocal

    limit = max(1, min(int(limit or 5), 50))
    own = db is None
    db = db or SessionLocal()
    try:
        query = db.query(ChatMessage.id, ChatMessage.session_id, ChatMessage.timestamp, ChatMessage.meta_data) \
            .filter(ChatMessage.role == "assistant") \
            .filter(ChatMessage.meta_data.like('%"tools_offered"%'))
        if message_id:
            query = query.filter(ChatMessage.id == str(message_id))
        elif session_id:
            query = query.filter(ChatMessage.session_id == str(session_id))
        elif owner:
            query = query.join(Session, Session.id == ChatMessage.session_id).filter(Session.owner == owner)
        rows = query.order_by(ChatMessage.timestamp.desc()).limit(limit).all()
        turns = []
        for mid, sid, ts, meta in rows:
            offered = _metadata(meta).get("tools_offered")
            if isinstance(offered, dict):
                turns.append({"message_id": mid, "session_id": sid,
                              "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts), **offered})
        return {"turns": turns, "count": len(turns),
                "note": "count/tokens: distinct tools offered over the turn; sent_tokens: summed over its rounds."}
    finally:
        if own:
            db.close()


__all__ = ["TOP", "costs_of", "peek", "record", "report", "summarize", "take"]
