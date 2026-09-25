"""Context tools: the model managing its own context in a long agent run.

``context_status`` / ``context_pin`` / ``context_unpin`` / ``context_drop`` /
``context_note``. A handler never sees the conversation: it validates the
arguments and returns an *intent* under ``context_intent``; the agent loop
applies it to the live messages right after the call
(``src.context_self_manage.apply_intent``) and replaces this result with what
actually happened. Outside an agent turn nothing applies the intent, so the
``output`` here says so instead of pretending.

Offered only when a run gets long (usage or round thresholds, see
``agent_context_tools_offer_pct`` / ``agent_context_tools_offer_round``) or
when the user asks — never on every turn. Docs: docs/api/context_tools.md.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.context_self_manage import INTENT_KEY, MAX_NOTE_CHARS, parse_args

_OUTSIDE = "context tools act on the live agent turn; nothing was changed here."


def _bad(tool: str, why: str) -> Dict[str, Any]:
    return {"error": f"{tool}: {why}", "exit_code": 1}


def _handles(args: Dict[str, Any]) -> Optional[List[str]]:
    raw = args.get("handles", args.get("handle"))
    if raw is None:
        return []
    if isinstance(raw, str):
        return [h for h in raw.replace(",", " ").split() if h]
    if isinstance(raw, list) and all(isinstance(h, (str, int)) for h in raw):
        return [str(h) for h in raw if str(h).strip()]
    return None


def _intent(op: str, **fields: Any) -> Dict[str, Any]:
    return {INTENT_KEY: {"op": op, **fields}, "output": _OUTSIDE, "exit_code": 0}


class ContextStatusTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = parse_args(content)
        if args is None:
            return _bad("context_status", "JSON object required")
        top = args.get("top")
        if top is not None:
            try:
                top = max(1, min(30, int(top)))
            except (TypeError, ValueError):
                return _bad("context_status", "`top` must be an integer")
        return _intent("status", **({"top": top} if top is not None else {}))


class ContextPinTool:
    def __init__(self, unpin: bool = False):
        self.unpin = unpin
        self.name = "context_unpin" if unpin else "context_pin"

    async def execute(self, content: str, ctx: dict) -> dict:
        args = parse_args(content)
        if args is None:
            return _bad(self.name, "JSON object required")
        handles = _handles(args)
        if handles is None:
            return _bad(self.name, "`handles` must be a list of strings")
        snippet = str(args.get("snippet") or "").strip()
        everything = bool(args.get("all")) if self.unpin else False
        if not handles and not snippet and not everything:
            return _bad(self.name, "pass `handles` (from context_status) or a unique `snippet`")
        fields: Dict[str, Any] = {"handles": handles}
        if snippet:
            fields["snippet"] = snippet
        if everything:
            fields["all"] = True
        return _intent("unpin" if self.unpin else "pin", **fields)


class ContextDropTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = parse_args(content)
        if args is None:
            return _bad("context_drop", "JSON object required")
        handles = _handles(args)
        if not handles:
            return _bad("context_drop", "`handles` (from context_status) is required")
        return _intent("drop", handles=handles)


class ContextNoteTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        args = parse_args(content)
        if args is None:
            return _bad("context_note", "JSON object required")
        handles = _handles(args)
        if not handles:
            return _bad("context_note", "`handles` (from context_status) is required")
        note = str(args.get("note") or "").strip()
        if not note:
            return _bad("context_note", "`note` is required: what you keep from those results")
        if len(note) > MAX_NOTE_CHARS:
            return _bad("context_note", f"`note` is longer than {MAX_NOTE_CHARS} characters")
        return _intent("note", handles=handles, note=note)
