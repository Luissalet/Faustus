"""src/tool_arg_rehearsal.py - try argument rules against calls already made.

The Settings "Test" box answers for ONE tool and ONE set of arguments, typed
by hand. Before a new rule is switched on the useful question is a different
one: *what would it have done to the work I actually do?* A rule meant to keep
``web_fetch`` on two domains that would also have stopped forty of last
week's legitimate fetches is a rule to rewrite before saving, not after.

So this module replays recorded tool calls through
:func:`src.tool_arg_policy.evaluate_rules` with a candidate rule list, and
counts what would have been denied or sent to approval, per rule, with a few
examples. Nothing runs and nothing is saved: it is read-only by construction.

The corpus is two ledgers, newest first:

* chat ``tool_events`` on assistant messages (Faustus's own tools);
* ``agent_gate`` receipts in the shared command-guard chain (foreign runners:
  session ``agent-gate:*``), so a rule about ``bash`` is also checked against
  what Claude Code / Codex tried under the gate.

When a rule would have stopped a call that the gate had already blocked or
sent to approval, that count is reported separately
(``already_stopped_by_other_gate``): the rule is redundant for those calls,
not wrong.

Limits, stated so the numbers are read right:

* the arguments come from what the tool event recorded (``command``), or from
  the gate receipt's ``command_head`` (the chain only keeps a head + hash),
  parsed with the same :func:`src.tool_arg_policy.extract_tool_args` the live
  gate uses, so a rule on ``command`` for ``bash`` sees what execution saw;
* only the FIRST rule that fires counts for a call, exactly as at run time;
* a call that some other gate already refused is still in the history and is
  counted like any other - the question is what the rules would do - and the
  overlap with a prior stop is reported, not used to hide the rule hit.
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

#: Receipts written by :func:`src.agent_gate.record` use this session prefix.
GATE_SESSION_PREFIX = "agent-gate:"
#: Gate actions that already stopped the call (another door fired first).
GATE_STOPPED_ACTIONS = frozenset({"blocked", "asked"})
#: Claude Code / Codex tool names → Faustus builtins the arg rules use.
_GATE_TOOL_ALIASES = {
    "Bash": "bash",
    "bash": "bash",
    "Write": "write_file",
    "Edit": "edit_file",
    "MultiEdit": "edit_file",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "Read": "read_file",
}


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
        out.append({"tool": tool, "args": _text_value(event.get("command")),
                    "source": "chat"})
    return out


def _gate_tool_name(raw: str) -> str:
    """``runner:Bash`` / ``Bash`` → the Faustus tool name arg rules match."""
    name = str(raw or "").strip()
    if ":" in name:
        name = name.split(":", 1)[1].strip() or name
    return _GATE_TOOL_ALIASES.get(name, name)


def calls_from_gate_receipts(receipts: Any = None, *, limit: Any = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    """Foreign-runner tool calls from the shared command-guard receipt chain.

    Reads :func:`src.command_guard.tail_receipts` when ``receipts`` is omitted.
    Newest first. Never raises.
    """
    limit = _clamp_limit(limit)
    if receipts is None:
        try:
            from src import command_guard
            # Fetch a wider window: chat + gate share the same limit budget.
            receipts = command_guard.tail_receipts(min(1000, max(limit * 2, 100)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool-arg rehearsal could not read gate receipts: %r", exc)
            return []
    out: List[Dict[str, Any]] = []
    for row in receipts or ():
        if not isinstance(row, dict):
            continue
        session = str(row.get("session") or "")
        if not session.startswith(GATE_SESSION_PREFIX):
            continue
        tool = _gate_tool_name(str(row.get("tool") or ""))
        if not tool:
            continue
        prior = str(row.get("action") or "")
        out.append({
            "tool": tool,
            "args": str(row.get("command_head") or ""),
            "ts": str(row.get("ts") or ""),
            "session_id": session,
            "source": "agent_gate",
            "prior_action": prior,
            "runner_tool": str(row.get("tool") or ""),
        })
    out.sort(key=lambda c: str(c.get("ts") or ""), reverse=True)
    return out[:limit]


def recent_tool_calls(limit: Any = DEFAULT_LIMIT, db: Any = None,
                      *, include_gate: bool = True) -> List[Dict[str, Any]]:
    """The newest recorded tool calls, newest first. Never raises.

    When ``include_gate`` is true (default), merges chat ``tool_events`` with
    :func:`calls_from_gate_receipts` and sorts by ``ts`` descending.
    """
    limit = _clamp_limit(limit)
    chat: List[Dict[str, Any]] = []
    try:
        from core.database import ChatMessage, SessionLocal
    except Exception:  # noqa: BLE001
        SessionLocal = None  # type: ignore[misc, assignment]
        ChatMessage = None  # type: ignore[misc, assignment]
    else:
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
            for session_id, timestamp, meta in rows:
                for call in calls_from_metadata(meta):
                    call["session_id"] = str(session_id or "")
                    call["ts"] = (timestamp.isoformat() if hasattr(timestamp, "isoformat")
                                  else str(timestamp or ""))
                    chat.append(call)
                    if len(chat) >= limit:
                        break
                if len(chat) >= limit:
                    break
        except Exception as exc:  # noqa: BLE001 - a rehearsal never breaks Settings
            logger.warning("tool-arg rehearsal could not read the history: %r", exc)
        finally:
            if own and db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass

    if not include_gate:
        return chat[:limit]

    gate = calls_from_gate_receipts(limit=limit)
    merged = chat + gate
    merged.sort(key=lambda c: str(c.get("ts") or ""), reverse=True)
    return merged[:limit]


def rehearse(rules: Any, calls: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """What ``rules`` would have done to ``calls``.

    ``{"checked", "would_deny", "would_ask", "untouched", "by_rule",
    "samples", "external_checked", "already_stopped_by_other_gate"}`` —
    ``by_rule`` keeps the rule order, every rule listed even at zero so a
    rule that never fires is visible as such.
    """
    rule_list = [r for r in (rules or []) if isinstance(r, dict)]
    by_rule: Dict[str, Dict[str, Any]] = {}
    for rule in rule_list:
        rid = str(rule.get("id") or "")
        by_rule.setdefault(rid, {"id": rid, "tool": str(rule.get("tool") or ""),
                                 "deny": 0, "ask": 0})
    checked = deny = ask = 0
    external_checked = 0
    already_stopped = 0
    samples: List[Dict[str, Any]] = []
    for call in calls or ():
        if not isinstance(call, dict):
            continue
        tool = str(call.get("tool") or "")
        if not tool:
            continue
        checked += 1
        source = str(call.get("source") or "chat")
        if source == "agent_gate":
            external_checked += 1
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
        prior = str(call.get("prior_action") or "")
        stopped_already = prior in GATE_STOPPED_ACTIONS
        if stopped_already:
            already_stopped += 1
        bucket = by_rule.setdefault(decision.rule_id, {"id": decision.rule_id, "tool": "",
                                                       "deny": 0, "ask": 0})
        bucket["ask" if decision.action == "ask" else "deny"] += 1
        if len(samples) < MAX_SAMPLES:
            head = "" if raw_args is None else str(raw_args)
            sample = {
                "ts": str(call.get("ts") or ""),
                "session_id": str(call.get("session_id") or ""),
                "tool": tool,
                "action": decision.action,
                "rule_id": decision.rule_id,
                "message": decision.message(),
                "args_head": head[:ARGS_HEAD_CHARS] + ("\u2026" if len(head) > ARGS_HEAD_CHARS else ""),
                "source": source,
                "already_stopped_by_other_gate": stopped_already,
            }
            if prior:
                sample["prior_action"] = prior
            samples.append(sample)
    return {
        "checked": checked,
        "would_deny": deny,
        "would_ask": ask,
        "untouched": checked - deny - ask,
        "by_rule": list(by_rule.values()),
        "samples": samples,
        "external_checked": external_checked,
        "already_stopped_by_other_gate": already_stopped,
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
