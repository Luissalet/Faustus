"""`criteria_check`: link the turn's contract criteria to the tool calls that
showed them (src/contract_criteria.py).

The model proposes a link; the harness decides the state
(`src.verification.verify_contract_link`): the referenced call must exist in
this turn, have run after the last change to the workspace, and mention the
criterion's subject and terms. Nothing the model writes here can move a
criterion to `proven` on its own.

The book of the turn travels in the tool context as `ctx["contract_book"]`
(set by src/agent_loop.py through the turn options, src/tool_execution.py).
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional


def _parse(content: str) -> Optional[Dict[str, Any]]:
    text = (content or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class CriteriaCheckTool:
    """`{"links": [{"id", "status"?, "call_id"? | "command"? | "test"?, "reason"?,
    "expected"?, "observed"?}]}` or `{"list": true}`."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse(content)
        if args is None:
            return {"error": "criteria_check: JSON object required", "exit_code": 1}
        book = (ctx or {}).get("contract_book")
        if book is None:
            return {"error": "criteria_check: this turn has no contract criteria", "exit_code": 1}
        links = args.get("links")
        if isinstance(links, dict):
            links = [links]
        if not links and args.get("id"):
            links = [args]
        if not links:
            if not args.get("list"):
                return {"error": "criteria_check: 'links' required (or {\"list\": true})", "exit_code": 1}
            return {
                "output": book.compact_list(max_chars=12000, quote_chars=300) + "\n\n" + _counts_line(book),
                "exit_code": 0,
                "criteria": book.counts(),
                "open": [c["id"] for c in book.open()],
            }
        if not isinstance(links, list):
            return {"error": "criteria_check: 'links' must be a list", "exit_code": 1}
        rows = book.apply_links(links[:80])
        book.save()
        lines = []
        for r in rows:
            if r.get("error"):
                lines.append(f"{r.get('id')}: not applied - {r['error']}")
            elif r.get("accepted") is False:
                lines.append(f"{r['id']}: {r['state']} - NOT accepted: {r.get('detail')}")
            else:
                lines.append(f"{r['id']}: {r['state']} ({r.get('detail')})")
        if any(r.get("accepted") is False or r.get("error") for r in rows):
            recent = book.recent_calls()
            if recent:
                lines.append("Recent calls of this turn: " + " ; ".join(recent))
        lines.append(_counts_line(book))
        return {
            "output": "\n".join(lines),
            "exit_code": 0,
            "results": rows,
            "criteria": book.counts(),
            "open": [c["id"] for c in book.open()],
        }


def _counts_line(book) -> str:
    n = book.counts()
    open_ids = [c["id"] for c in book.open()]
    tail = (" Open: " + ", ".join(open_ids[:30]) + (" ..." if len(open_ids) > 30 else "")) if open_ids else ""
    return (f"{n['proven']} proven, {n['pending']} pending, {n['unmet']} unmet, "
            f"{n['not_applicable']} not applicable of {n['total']}.{tail}")
