"""What a whole chat cost: tokens, prompt cache, steps, tools, time, money.

Every assistant message already carries its turn's metrics (the same dict the
turn timeline shows). This folds them into one summary per chat, per model,
so a person can see at a glance what a long session spent and how much of
its prompt the cache served: the numbers a hosted-model bill is made of,
and the ones that say whether the local engine is reusing its prefix.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _add(bucket: Dict[str, Any], key: str, value: Any) -> None:
    v = _num(value)
    if v is not None:
        bucket[key] = bucket.get(key, 0) + v


def summarize(messages: Iterable[Any]) -> Dict[str, Any]:
    """`messages`: ChatMessage objects or dicts with `role` and `metadata`."""
    total: Dict[str, Any] = {"turns": 0}
    by_model: Dict[str, Dict[str, Any]] = {}
    last_context: Optional[Dict[str, Any]] = None
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        md = (m.get("metadata") if isinstance(m, dict) else getattr(m, "metadata", None)) or {}
        if role != "assistant" or not isinstance(md, dict):
            continue
        if not any(k in md for k in ("input_tokens", "output_tokens", "agent_rounds", "response_time")):
            continue
        model = str(md.get("model") or md.get("requested_model") or "unknown")
        row = by_model.setdefault(model, {"turns": 0})
        for bucket in (total, row):
            bucket["turns"] += 1
            _add(bucket, "input_tokens", md.get("input_tokens"))
            _add(bucket, "output_tokens", md.get("output_tokens"))
            _add(bucket, "steps", md.get("agent_rounds"))
            _add(bucket, "tool_calls", md.get("tool_calls"))
            _add(bucket, "time_s", md.get("total_time") or md.get("response_time"))
            _add(bucket, "cost_usd", md.get("cost_usd"))
            pc = md.get("prompt_cache")
            if isinstance(pc, dict):
                _add(bucket, "prompt_processed", pc.get("processed"))
                _add(bucket, "prompt_cached", pc.get("cached"))
                _add(bucket, "cache_lost_rounds", pc.get("lost_rounds"))
        if md.get("context_length"):
            last_context = {"context_length": md.get("context_length"),
                            "context_percent": md.get("context_percent"),
                            "request_context_tokens": md.get("request_context_tokens")}
    for bucket in [total, *by_model.values()]:
        for k, v in list(bucket.items()):
            if isinstance(v, float) and v.is_integer() and k not in ("cost_usd", "time_s"):
                bucket[k] = int(v)
        if "time_s" in bucket:
            bucket["time_s"] = round(bucket["time_s"], 1)
        if "cost_usd" in bucket:
            bucket["cost_usd"] = round(bucket["cost_usd"], 6)
        seen = (bucket.get("prompt_processed") or 0) + (bucket.get("prompt_cached") or 0)
        if seen:
            bucket["cache_hit_percent"] = round(100.0 * (bucket.get("prompt_cached") or 0) / seen, 1)
    return {"total": total, "by_model": by_model, "context": last_context}
