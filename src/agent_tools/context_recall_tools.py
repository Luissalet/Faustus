"""agent_tools/context_recall_tools.py — the `context_recall` tool executor.

OBJ-29. The live context packet ends with a footer listing what the compiler
left out for budget, one line per item: ``[ctx:<id>] <title> (<source_ref>)``.
This tool is the other half: it takes those ids and returns the full text,
with where it came from and why it had been left out
(`src/context_engine/recall.py`).

    context_recall  read  {ids: [str]} -> full content + provenance per id

The owner comes from the tool context (the runtime), never from the
arguments: an id from another owner's footer resolves to "not found", the
same answer an expired or invented id gets.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_ID_IN_TEXT = re.compile(r"(?:ctx:)?([0-9a-fA-F]{10})\b")


def _ids(content: Any) -> List[str]:
    """Permissive: ``{"ids": [...]}``, ``{"id": "..."}``, a JSON list, or bare
    text with ``ctx:<id>`` tokens in it (a model that pasted the footer line
    still meant those ids)."""
    raw: Any = content
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except (TypeError, ValueError):
            return _ID_IN_TEXT.findall(text)
    if isinstance(raw, dict):
        value = raw.get("ids")
        if value is None:
            value = raw.get("id")
        raw = value
    if isinstance(raw, str):
        return _ID_IN_TEXT.findall(raw) or [raw]
    if isinstance(raw, list):
        return [str(v) for v in raw if isinstance(v, (str, int))]
    return []


class ContextRecallTool:
    """`context_recall` {ids: [str]}: bring back items the context packet
    omitted for budget, in full, with provenance. Read tool."""

    async def execute(self, content: Any, ctx: dict) -> Dict[str, Any]:
        ids = _ids(content)
        if not ids:
            return {"error": "context_recall: `ids` is required — the ctx:<id> "
                             "values from the packet's omitted_for_budget list",
                    "exit_code": 1}
        try:
            from src.context_engine import recall
        except Exception as exc:  # noqa: BLE001
            return {"error": f"context_recall: the context engine is unavailable: {exc}",
                    "exit_code": 1}
        owner = str((ctx or {}).get("owner") or "")
        results = await recall.recall(ids, owner=owner)
        found = sum(1 for r in results if r.get("found"))
        return {
            "output": recall.render_recall(results) or "nothing recalled",
            "items": results,
            "found": found,
            "requested": len(ids),
            "exit_code": 0 if found else 1,
        }


__all__ = ["ContextRecallTool"]
