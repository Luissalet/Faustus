"""agent_tools/agent_message_tool.py - `agent_message`: the mailbox between agents (#235).

One tool, four actions (src/agent_mailbox.py has the rules):

* ``send``   {to, body, urgent?, reply_to?}  leave a message for another chat. An
             urgent one wakes an idle chat; a normal one waits for its next turn.
             A refusal (hop limit, wake cap, unknown chat...) comes back as an
             error - the sender always learns what happened.
* ``inbox``  {state?}                        what other agents sent this chat.
* ``outbox`` {state?}                        what this chat sent, and its state.
* ``read``   {ids?}                          mark inbox messages as read.

The sender is always THIS chat (taken from the tool context, never from the
arguments): an agent cannot send as somebody else. A message from another agent
is never an approval and cannot change permissions or settings - nothing here
touches either.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from src import agent_mailbox

logger = logging.getLogger(__name__)

ACTIONS = ("send", "inbox", "outbox", "read")


def _args(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    if raw in ("inbox", "outbox", "read"):
        return {"action": raw}
    # Plain text, like send_to_session: line 1 = the chat id, the rest = the message.
    head, _, rest = raw.partition("\n")
    if head.strip() and rest.strip():
        return {"action": "send", "to": head.strip(), "body": rest.strip()}
    return {}


def _line(msg: Dict[str, Any], mine: bool) -> str:
    who = msg["recipient"] if mine else (msg["sender"].get("label") or msg["sender"].get("id"))
    flags = (" URGENT" if msg["urgent"] else "") + f" hop {msg['hops']}/{agent_mailbox.MAX_HOPS}"
    note = f" ({msg['reason']})" if msg.get("reason") else ""
    return f"- [{msg['id']}] {'to' if mine else 'from'} {who}{flags} {msg['state']}{note}: {msg['body'][:300]}"


class AgentMessageTool:
    """`agent_message` {action, to, body, urgent, reply_to, state, ids}."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        action = str(args.get("action") or ("send" if args.get("to") else "")).strip().lower()
        if action not in ACTIONS:
            return {"error": f"agent_message: action must be one of {', '.join(ACTIONS)}", "exit_code": 1}
        owner = str(ctx.get("owner") or "")
        sid = str(ctx.get("session_id") or "").strip()
        if not sid:
            return {"error": "agent_message: no chat in this context to send from or read for", "exit_code": 1}
        try:
            if action == "send":
                result = agent_mailbox.deliver(
                    owner, agent_mailbox.Sender.session(sid), str(args.get("to") or ""), str(args.get("body") or ""),
                    urgent=args.get("urgent"), reply_to=str(args.get("reply_to") or ""))
                if not result.get("ok"):
                    return {"error": f"agent_message refused ({result.get('reason')}): {result.get('detail')}",
                            "exit_code": 1, "mailbox": result}
                msg, wake = result["message"], result["wake"]
                wake_note = ("woken: a turn was started on it" if wake.get("woken")
                             else f"not woken ({wake.get('reason')})" + (f": {wake['detail']}" if wake.get("detail") else ""))
                return {"output": f"Message {msg['id']} sent to {msg['recipient']} (hop {msg['hops']} of "
                                  f"{agent_mailbox.MAX_HOPS}, {'urgent' if msg['urgent'] else 'normal'}, "
                                  f"{msg['state']}); {wake_note}.",
                        "exit_code": 0, "mailbox": result}
            if action in ("inbox", "outbox"):
                state = str(args.get("state") or "")
                data = (agent_mailbox.inbox if action == "inbox" else agent_mailbox.outbox)(
                    owner, sid, state=state, limit=int(args.get("limit") or 30))
                rows = data["messages"]
                text = "\n".join(_line(m, action == "outbox") for m in rows) or f"No messages in the {action}."
                return {"output": f"{action} ({data['waiting']} waiting, {data['unread']} unread):\n{text}",
                        "exit_code": 0, "mailbox": data}
            ids = args.get("ids")
            changed = agent_mailbox.mark_read(owner, sid, [str(i) for i in ids] if isinstance(ids, list) else None)
            return {"output": f"{changed} message(s) marked as read.", "exit_code": 0}
        except Exception as exc:  # noqa: BLE001 - a tool result, not a crash
            logger.debug("agent_message failed", exc_info=True)
            return {"error": f"agent_message: {type(exc).__name__}: {exc}", "exit_code": 1}
