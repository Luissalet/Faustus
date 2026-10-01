"""src/tool_arg_rehearsal.py - try argument rules against calls already made.

The Settings "Test" box answers for ONE tool and ONE set of arguments, typed
by hand. Before a new rule is switched on the useful question is a different
one: *what would it have done to the work I actually do?* A rule meant to keep
``web_fetch`` on two domains that would also have stopped forty of last
week's legitimate fetches is a rule to rewrite before saving, not after.

So this module replays the recorded tool calls (the ``tool_events`` every
assistant message keeps in its metadata, newest first) through
:func:`src.tool_arg_policy.evaluate_rules` with a candidate rule list, and
counts what would have been denied or sent to approval, per rule, with a few
examples. Nothing runs and nothing is saved: it is read-only by construction.

Limits, stated so the numbers are read right:

* the arguments come from what the tool event recorded (``command``), parsed
  with the same :func:`src.tool_arg_policy.extract_tool_args` the live gate
  uses, so a rule on ``command`` for ``bash`` sees what execution saw;
* only the FIRST rule that fires counts for a call, exactly as at run time;
* a call that some other gate already refused is still in the history and is
  counted like any other - the question is what the rules would do.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional

from src.tool_arg_policy import evaluate_rules, extract_tool_args

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 500
MAX_LIMIT = 5000
MAX_SAMPLES = 12
ARGS_HEAD_CHARS = 160
# How many assistant messages are read to collect `limit` calls. A message
# usually carries several calls, so this is generous without reading the
# whole database.
_MESSAGES_PER_CALL = 1


def _clamp_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def calls_from_metadata(meta: Any) -> List[Dict[str, str]]:
    """``[{"tool", "args"}]`` from one message's metadata (dict or JSON)."""
    if isinstance(meta, str):
        try:
            meta = json.loads(meta or "{}")
        except (TypeError, ValueError):
            return []
    if not isinstance(meta, dict):
        return []
    events = meta.get("tool_events")
    if not isinstance(events, list):
        return []
    from src.chat_export import _resolved_tool_name, _text_value

    out: List[Dict[str, str]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        tool = _resolved_tool_name(event)
        if not tool:
            continue
        out.append({"tool": tool, "args": _text_value(event.get("command"))})
    return out


def recent_tool_calls(limit: Any = DEFAULT_LIMIT, db: Any = None) -> List[Dict[str, Any]]:
    """The newest recorded tool calls, newest first. Never raises."""
    limit = _clamp_limit(limit)
    try:
        from core.database import ChatMessage, SessionLocal
    except Exception:  # noqa: BLE001
        return []
    own = db is None
    try:
        if own:
            db = SessionLocal()
        rows = (
            db.query(ChatMessage.session_id, ChatMessage.timestamp, ChatMessage.meta_data)
            .filter(ChatMessage.role == "assistant")
            .filter(ChatMessage.meta_data.like("%tool_events%"))
            .order_by(ChatMessage.timestamp.desc())
            .limit(max(50, limit * _MESSAGES_PER_CALL))
            .all()
        )
    except Exception as exc:  # noqa: BLE001 - a rehearsal never breaks Settings
        logger.warning("tool-arg rehearsal could not read the history: %r", exc)
        return []
    finally:
        if own and db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
    calls: List[Dict[str, Any]] = []
    for session_id, timestamp, meta in rows:
        for call in calls_from_metadata(meta):
            call["session_id"] = str(session_id or "")
            call["ts"] = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp or "")
            calls.append(call)
            if len(calls) >= limit:
                return calls
    return calls


def rehearse(rules: Any, calls: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """What ``rules`` would have done to ``calls``.

    ``{"checked", "would_deny", "would_ask", "untouched", "by_rule",
    "samples"}`` - ``by_rule`` keeps the rule order, every rule listed even at
    zero so a rule that never fires is visible as such.
    """
    rule_list = [r for r in (rules or []) if isinstance(r, dict)]
    by_rule: Dict[str, Dict[str, Any]] = {}
    for rule in rule_list:
        rid = str(rule.get("id") or "")
        by_rule.setdefault(rid, {"id": rid, "tool": str(rule.get("tool") or ""),
                                 "deny": 0, "ask": 0})
    checked = deny = ask = 0
    samples: List[Dict[str, Any]] = []
    for call in calls or ():
        if not isinstance(call, dict):
            continue
        tool = str(call.get("tool") or "")
        if not tool:
            continue
        checked += 1
        raw_args = call.get("args")
        try:
            args = extract_tool_args(tool, raw_args)
        except Exception:  # noqa: BLE001
            args = {"content": "" if raw_args is None else str(raw_args)}
        decision = evaluate_rules(tool, args, rule_list)
        if decision is None:
            continue
        if decision.action == "ask":
            ask += 1
        else:
            deny += 1
        bucket = by_rule.setdefault(decision.rule_id, {"id": decision.rule_id, "tool": "",
                                                       "deny": 0, "ask": 0})
        bucket["ask" if decision.action == "ask" else "deny"] += 1
        if len(samples) < MAX_SAMPLES:
            head = "" if raw_args is None else str(raw_args)
            samples.append({
                "ts": str(call.get("ts") or ""),
                "session_id": str(call.get("session_id") or ""),
                "tool": tool,
                "action": decision.action,
                "rule_id": decision.rule_id,
                "message": decision.message(),
                "args_head": head[:ARGS_HEAD_CHARS] + ("\u2026" if len(head) > ARGS_HEAD_CHARS else ""),
            })
    return {
        "checked": checked,
        "would_deny": deny,
        "would_ask": ask,
        "untouched": checked - deny - ask,
        "by_rule": list(by_rule.values()),
        "samples": samples,
    }


def rehearse_recent(rules: Any, limit: Any = DEFAULT_LIMIT,
                    calls: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """:func:`rehearse` over :func:`recent_tool_calls`, with the window size."""
    limit = _clamp_limit(limit)
    history = calls if calls is not None else recent_tool_calls(limit)
    result = rehearse(rules, history[:limit])
    result["limit"] = limit
    result["oldest"] = history[min(len(history), limit) - 1].get("ts", "") if history else ""
    result["newest"] = history[0].get("ts", "") if history else ""
    return result
