"""What the owner used over a period, across every chat: turns, tokens,
prompt cache, steps, tool calls, time, cost, per model; local vs hosted;
the tools used most; the busiest days.

Folds the same per-turn metrics the chats already saved (see
src/session_usage.py), read straight from the database for the period, so a
month's recap costs one query and no model call.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.session_usage import summarize


def _rows(owner: Optional[str], since: datetime) -> List[Dict[str, Any]]:
    from core.database import ChatMessage, Session as DbSession, SessionLocal
    db = SessionLocal()
    try:
        q = (db.query(ChatMessage.meta_data, ChatMessage.timestamp, ChatMessage.session_id, DbSession.endpoint_url)
             .join(DbSession, ChatMessage.session_id == DbSession.id)
             .filter(ChatMessage.role == "assistant", ChatMessage.timestamp >= since))
        if owner:
            q = q.filter(DbSession.owner == owner)
        out = []
        for meta, ts, sid, url in q.all():
            try:
                md = json.loads(meta) if meta else {}
            except (TypeError, ValueError):
                md = {}
            if isinstance(md, dict) and md:
                out.append({"metadata": md, "ts": ts, "session_id": sid, "endpoint_url": url or ""})
        return out
    finally:
        db.close()


def recap(owner: Optional[str], days: int = 30, *, rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    days = max(1, min(int(days or 30), 366))
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    rows = rows if rows is not None else _rows(owner, since)
    msgs = [{"role": "assistant", "metadata": r["metadata"]} for r in rows]
    summary = summarize(msgs)
    try:
        from src.model_context import is_local_endpoint
    except Exception:  # noqa: BLE001
        is_local_endpoint = None  # type: ignore
    local = hosted = 0
    tools: Counter = Counter()
    by_day: Counter = Counter()
    chats = set()
    for r in rows:
        md = r["metadata"]
        if not any(k in md for k in ("input_tokens", "output_tokens", "agent_rounds", "response_time")):
            continue
        chats.add(r["session_id"])
        url = r.get("endpoint_url") or ""
        if is_local_endpoint is not None and url:
            try:
                if is_local_endpoint(url):
                    local += 1
                else:
                    hosted += 1
            except Exception:  # noqa: BLE001
                pass
        for ev in md.get("tool_events") or []:
            if isinstance(ev, dict) and ev.get("tool"):
                tools[str(ev["tool"])] += 1
        ts = r.get("ts")
        if isinstance(ts, datetime):
            by_day[ts.date().isoformat()] += 1
    return {
        "days": days, "since": since.isoformat(timespec="seconds"),
        "chats": len(chats), "turns_local": local, "turns_hosted": hosted,
        "total": summary["total"], "by_model": summary["by_model"],
        "top_tools": tools.most_common(10),
        "busiest_days": sorted(by_day.items(), key=lambda kv: (-kv[1], kv[0]))[:5],
        "generated_at": int(time.time()),
    }


def render(data: Dict[str, Any]) -> str:
    t = data.get("total") or {}
    if not t.get("turns"):
        return f"No turns with saved metrics in the last {data.get('days')} days."
    lines = [f"### Last {data['days']} days",
             f"- {t['turns']} turns in {data['chats']} chats · local {data['turns_local']} · hosted {data['turns_hosted']}",
             f"- {t.get('input_tokens', 0):,} input and {t.get('output_tokens', 0):,} output tokens"
             + (f" · {t['cache_hit_percent']} % of the prompt from cache" if t.get("cache_hit_percent") is not None else "")]
    if t.get("steps") or t.get("tool_calls"):
        lines.append(f"- {t.get('steps', 0)} model steps · {t.get('tool_calls', 0)} tool calls"
                     + (f" · {t['time_s']} s of model time" if t.get("time_s") else ""))
    if t.get("cost_usd"):
        lines.append(f"- cost ${t['cost_usd']}")
    if data.get("by_model"):
        lines.append("- models: " + ", ".join(f"`{m}` {b.get('turns', 0)}" for m, b in
                                              sorted(data["by_model"].items(), key=lambda kv: -kv[1].get("turns", 0))[:6]))
    if data.get("top_tools"):
        lines.append("- top tools: " + ", ".join(f"{name} {n}" for name, n in data["top_tools"][:8]))
    if data.get("busiest_days"):
        lines.append("- busiest days: " + ", ".join(f"{d} ({n})" for d, n in data["busiest_days"]))
    return "\n".join(lines)
