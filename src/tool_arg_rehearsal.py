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
* ``agent_gate`` receipts in the shared command-guard chain (external
  runners: session ``agent-gate:*``), so a rule about ``bash`` is also
  checked against what those runners tried under the gate.

When a rule would have stopped a call that the gate had already blocked or
sent to approval, that count is reported separately
(``already_stopped_by_other_gate``): the rule is redundant for those calls,
not wrong.

Limits, stated so the numbers are read right:

* the arguments come from what the tool event recorded (``command``), or from
  the gate receipt's ``command_head`` (the chain only keeps a head + hash).
  Runner names are mapped to builtins and their argument keys are translated
  (``file_path`` → ``path``, …) so rules see the same names as live Faustus
  tools. Heads that look like JSON but do not parse (truncated receipts) are
  counted as ``unparsed`` and never scored as deny/ask/untouched;
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
#: External-runner tool names → Faustus builtins the arg rules use.
_GATE_TOOL_ALIASES = {
    "Bash": "bash",
    "bash": "bash",
    "Write": "write_file",
    "Edit": "edit_file",
    "MultiEdit": "edit_file",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "Read": "read_file",
    "Glob": "glob",
    "Grep": "grep",
}
#: Runner argument names → names the builtin arg rules match on.
_GATE_ARG_ALIASES: Dict[str, Dict[str, str]] = {
    "write_file": {"file_path": "path", "path": "path"},
    "edit_file": {"file_path": "path", "path": "path"},
    "read_file": {"file_path": "path", "path": "path"},
    "web_fetch": {"url": "url"},
    "web_search": {"query": "query"},
    "bash": {"command": "command"},
    "glob": {"pattern": "pattern", "path": "path"},
    "grep": {"pattern": "pattern", "path": "path"},
}
#: Primary arg a gate receipt for this builtin must carry after translation.
_GATE_PRIMARY_ARG = {
    "web_fetch": "url",
    "web_search": "query",
    "write_file": "path",
    "edit_file": "path",
    "read_file": "path",
    "bash": "command",
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


def _translate_runner_args(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Map runner keys (``file_path``) onto builtin rule keys (``path``).

    Only keeps keys the builtin rules understand, so a long ``content`` /
    ``prompt`` field cannot displace the primary argument after truncation.
    """
    aliases = _GATE_ARG_ALIASES.get(tool)
    if not aliases:
        return dict(args)
    out: Dict[str, Any] = {}
    for key, value in args.items():
        mapped = aliases.get(key)
        if mapped and mapped not in out:
            out[mapped] = value
    return out


def gate_args_for_rehearsal(tool: str, raw_args: Any) -> tuple[Optional[Dict[str, Any]], bool]:
    """Parse a gate ``command_head`` into rule args.

    Returns ``(args, unparsed)``. When ``unparsed`` is true the head looked
    like structured JSON but could not be used (truncated / missing primary
    key): the caller must not score it as deny, ask, or untouched.
    """
    raw = "" if raw_args is None else str(raw_args)
    stripped = raw.strip()
    looked_like_json = stripped.startswith("{")
    parsed: Optional[Dict[str, Any]] = None
    if looked_like_json:
        try:
            value = json.loads(stripped)
        except (TypeError, ValueError):
            return None, True
        if not isinstance(value, dict):
            return None, True
        parsed = value
    else:
        try:
            parsed = extract_tool_args(tool, raw)
        except Exception:  # noqa: BLE001
            parsed = {"content": raw}
        if not isinstance(parsed, dict):
            parsed = {"content": raw}
    args = _translate_runner_args(tool, parsed)
    primary = _GATE_PRIMARY_ARG.get(tool)
    if looked_like_json and primary and primary not in args:
        # Truncated head kept prompt/content but lost url/file_path.
        return None, True
    if looked_like_json and not args and parsed:
        return None, True
    return args, False


def calls_from_gate_receipts(receipts: Any = None, *, limit: Any = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    """External-runner tool calls from the shared command-guard receipt chain.

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

    ``{"checked", "would_deny", "would_ask", "untouched", "unparsed",
    "by_rule", "samples", "external_checked", "already_stopped_by_other_gate"}`` —
    ``by_rule`` keeps the rule order, every rule listed even at zero so a
    rule that never fires is visible as such. ``unparsed`` gate heads are
    excluded from deny/ask/untouched so truncated receipts cannot inflate
    those numbers.
    """
    rule_list = [r for r in (rules or []) if isinstance(r, dict)]
    by_rule: Dict[str, Dict[str, Any]] = {}
    for rule in rule_list:
        rid = str(rule.get("id") or "")
        by_rule.setdefault(rid, {"id": rid, "tool": str(rule.get("tool") or ""),
                                 "deny": 0, "ask": 0})
    checked = deny = ask = unparsed = 0
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
        if source == "agent_gate":
            args, is_unparsed = gate_args_for_rehearsal(tool, raw_args)
            if is_unparsed or args is None:
                unparsed += 1
                continue
        else:
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
        "untouched": checked - deny - ask - unparsed,
        "unparsed": unparsed,
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
