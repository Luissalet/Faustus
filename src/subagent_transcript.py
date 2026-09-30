"""src/subagent_transcript.py — a read-only transcript of one sub-agent.

A worker of `delegate_agents` runs in its own chat session, so everything it
did is already stored: its messages, and on each assistant message the tool
calls and results it made (`metadata.tool_events`) plus that turn's token
counts. This module turns those rows into a compact, bounded, display-ready
structure, and folds the recorded model calls of the session (when call
tracing kept them) into a per-call token series for a heatmap.

Pure functions over plain dicts: the route does the database and the
ownership check, nothing here reads a session or writes anything.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional

MAX_CONTENT_CHARS = 20_000
MAX_TOOL_INPUT_CHARS = 1_500
MAX_TOOL_OUTPUT_CHARS = 6_000
MAX_TOOL_EVENTS_PER_MESSAGE = 200
MAX_CALLS = 500


def _clip(value: Any, limit: int) -> Dict[str, Any]:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return {"text": text, "truncated": False, "chars": len(text)}
    return {"text": text[:limit], "truncated": True, "chars": len(text)}


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return float(value)


def _int_or_none(value: Any) -> Optional[int]:
    v = _num(value)
    return int(v) if v is not None else None


def tool_event_view(event: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(event, dict) or not (event.get("tool") or event.get("command")):
        return None
    cmd = _clip(event.get("command") or event.get("desc"), MAX_TOOL_INPUT_CHARS)
    out = _clip(event.get("output"), MAX_TOOL_OUTPUT_CHARS)
    exit_code = event.get("exit_code")
    return {
        "tool": str(event.get("tool") or ""),
        "round": _int_or_none(event.get("round")),
        "input": cmd["text"], "input_truncated": cmd["truncated"],
        "output": out["text"], "output_truncated": out["truncated"], "output_chars": out["chars"],
        "exit_code": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
        "duration_ms": _int_or_none(event.get("duration_ms")),
        "model": str(event.get("model") or "") or None,
        "call_id": str(event.get("call_id") or "") or None,
    }


def message_view(index: int, role: str, content: Any, meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    meta = meta if isinstance(meta, dict) else {}
    text = content if isinstance(content, str) else ("" if content is None else str(content))
    clipped = _clip(text, MAX_CONTENT_CHARS)
    events: List[Dict[str, Any]] = []
    raw_events = meta.get("tool_events")
    dropped = 0
    if isinstance(raw_events, list):
        for ev in raw_events:
            view = tool_event_view(ev)
            if view is None:
                continue
            if len(events) >= MAX_TOOL_EVENTS_PER_MESSAGE:
                dropped += 1
                continue
            events.append(view)
    msg: Dict[str, Any] = {
        "index": index, "role": role,
        "content": clipped["text"], "content_truncated": clipped["truncated"], "content_chars": clipped["chars"],
        "timestamp": meta.get("timestamp"),
        "tool_events": events,
    }
    if dropped:
        msg["tool_events_omitted"] = dropped
    in_t, out_t = _int_or_none(meta.get("input_tokens")), _int_or_none(meta.get("output_tokens"))
    if in_t is not None or out_t is not None:
        msg["tokens"] = {"input": in_t or 0, "output": out_t or 0}
    if meta.get("model"):
        msg["model"] = str(meta["model"])
    return msg


def _call_tokens(usage: Any) -> Dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    in_t = _int_or_none(usage.get("input_tokens"))
    if in_t is None:
        in_t = _int_or_none(usage.get("prompt_tokens"))
    out_t = _int_or_none(usage.get("output_tokens"))
    if out_t is None:
        out_t = _int_or_none(usage.get("completion_tokens"))
    out: Dict[str, int] = {}
    if in_t is not None:
        out["input"] = in_t
    if out_t is not None:
        out["output"] = out_t
    cached = _int_or_none(usage.get("cached_tokens"))
    if cached is not None:
        out["cached"] = cached
    return out


def usage_summary(messages: Iterable[Dict[str, Any]], calls: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Token totals of the sub-agent. Per-call numbers come from the recorded
    model calls when there are any (`source: "trace"`); otherwise from the
    per-turn counts on the assistant messages (`source: "messages"`); with
    neither, `source: "none"` and zeros, never an estimate."""
    per_call: List[Dict[str, Any]] = []
    for c in (calls or [])[:MAX_CALLS]:
        tok = _call_tokens(c.get("usage"))
        if not tok:
            continue
        per_call.append({"seq": c.get("seq"), "model": c.get("model"), "duration_ms": c.get("duration_ms"),
                         "input": tok.get("input", 0), "output": tok.get("output", 0),
                         "cached": tok.get("cached", 0)})
    if per_call:
        source = "trace"
        total_in = sum(c["input"] for c in per_call)
        total_out = sum(c["output"] for c in per_call)
    else:
        rows = [m["tokens"] for m in messages if isinstance(m, dict) and m.get("tokens")]
        source = "messages" if rows else "none"
        total_in = sum(r["input"] for r in rows)
        total_out = sum(r["output"] for r in rows)
    peak = max([c["input"] + c["output"] for c in per_call] or [0])
    for c in per_call:
        c["heat"] = round((c["input"] + c["output"]) / peak, 3) if peak else 0.0
    return {"source": source, "input_tokens": total_in, "output_tokens": total_out,
            "total_tokens": total_in + total_out, "calls": len(per_call), "per_call": per_call}


def build(session_id: str, name: str, model: str, rows: List[Dict[str, Any]], *,
          offset: int, total: int, calls: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """`rows`: ``{"role", "content", "metadata"}`` dicts in order, starting at
    message number `offset` of `total`."""
    messages = []
    for i, row in enumerate(rows):
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        if meta.get("hidden"):
            continue
        messages.append(message_view(offset + i, str(row.get("role") or ""), row.get("content"), meta))
    return {
        "session_id": session_id, "name": name, "model": model,
        "offset": offset, "total": total, "returned": len(messages),
        "has_more_after": offset + len(rows) < total,
        "messages": messages,
        "usage": usage_summary(messages, calls),
    }
