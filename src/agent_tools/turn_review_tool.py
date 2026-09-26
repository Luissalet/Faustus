"""agent_tools/turn_review_tool.py — `turn_review`: diagnose recent turns of a chat."""
import json
import logging
from typing import Any, Dict

from src import turn_review

logger = logging.getLogger(__name__)


def _args(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {"turns": raw} if raw.isdigit() else {}


class TurnReviewTool:
    """`turn_review` {turns?, session_id?}: what the last turns of this chat
    (or another of the owner's chats) did — tools, failures, rounds, time,
    prompt cache — with findings (repeated failures, loops, lost cache)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        try:
            turns = max(1, min(int(args.get("turns") or 1), 10))
        except (TypeError, ValueError):
            turns = 1
        sid = str(args.get("session_id") or ctx.get("session_id") or "").strip()
        if not sid:
            return {"error": "turn_review: no chat to review (pass session_id)", "exit_code": 1}
        try:
            from src.ai_interaction import get_session_manager
            sm = get_session_manager()
            sess = sm.get_session(sid) if sm else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("turn_review: session lookup failed: %s", exc)
            sess = None
        if sess is None:
            return {"error": f"turn_review: chat {sid} not found", "exit_code": 1}
        owner = ctx.get("owner")
        if owner and getattr(sess, "owner", None) not in (None, "", owner):
            return {"error": f"turn_review: chat {sid} not found", "exit_code": 1}
        history = getattr(sess, "history", None) or getattr(sess, "messages", None) or []
        # The current turn has not been saved yet: the last saved assistant
        # message is the previous turn.
        reviews = turn_review.review(history, turns)
        return {"output": turn_review.render(reviews), "exit_code": 0, "reviews": reviews}
